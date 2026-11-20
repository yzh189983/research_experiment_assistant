"""外部检索：从 OpenAlex / arXiv 搜论文，从 Hugging Face / Zenodo 搜数据集链接。

只读公开 API，不需要密钥；任何失败都静默降级，不影响数据集收集主流程。
"""
import re
import xml.etree.ElementTree as ET

import requests

import config
from core import crypto, venue_rank

_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


def _cfg():
    c = (config.load_config() or {}).get("dataset_search") or {}
    return {
        "enabled": c.get("enabled", True),
        "timeout": float(c.get("timeout") or 8),
        "max_papers": int(c.get("max_papers") or 5),
        "max_links": int(c.get("max_links") or 4),
        "sources": c.get("sources") or ["openalex", "arxiv", "huggingface"],
        "prefer_top_venues": c.get("prefer_top_venues", True),
        "year_from": int(c.get("year_from") or 0),
        "year_to": int(c.get("year_to") or 0),
    }


def enabled():
    return bool(_cfg()["enabled"])


def _get(url, params, timeout):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": "rekit-kaka/1.0 (local research assistant)"})
        r.raise_for_status()
        return r
    except Exception as e:
        raise RuntimeError(crypto.redact(str(e))[:200])


def search_papers(query, limit=None, year_from=None, year_to=None):
    """搜相关论文：[{title, authors, year, url, abstract, source, cited_by, venue, venue_tier}]。"""
    cfg = _cfg()
    limit = int(limit or cfg["max_papers"])
    y0 = int(year_from or cfg["year_from"]) or 0
    y1 = int(year_to or cfg["year_to"]) or 0
    if not cfg["enabled"] or not (query or "").strip():
        return []
    out = []
    pool = max(limit * 3, 15)  # 多取一些，留出按 venue 筛选的余地
    if "openalex" in cfg["sources"]:
        try:
            params = {"search": query, "per-page": pool, "mailto": "rekit@localhost"}
            filters = []
            if y0:
                filters.append(f"from_publication_date:{y0}-01-01")
            if y1:
                filters.append(f"to_publication_date:{y1}-12-31")
            if filters:
                params["filter"] = ",".join(filters)
            r = _get("https://api.openalex.org/works", params, cfg["timeout"])
            for w in (r.json().get("results") or [])[:pool]:
                authors = ", ".join(
                    str((a.get("author") or {}).get("display_name") or "")
                    for a in (w.get("authorships") or [])[:3])
                abstract = _invert_abstract(w.get("abstract_inverted_index"))
                src = (w.get("primary_location") or {}).get("source") or {}
                venue = str(src.get("display_name") or "")
                out.append({
                    "title": str(w.get("title") or "").strip(),
                    "authors": authors,
                    "year": w.get("publication_year"),
                    "url": str(w.get("doi") or w.get("id") or ""),
                    "abstract": abstract[:1200],
                    "source": "OpenAlex",
                    "cited_by": w.get("cited_by_count") or 0,
                    "venue": venue,
                    "venue_tier": venue_rank.rank_venue(venue),
                })
        except Exception:
            pass
    if len(out) < limit and "arxiv" in cfg["sources"]:
        try:
            r = _get("https://export.arxiv.org/api/query",
                     {"search_query": f"all:{query}", "start": 0,
                      "max_results": limit, "sortBy": "relevance"},
                     cfg["timeout"])
            root = ET.fromstring(r.content)
            for entry in root.findall("a:entry", _ARXIV_NS):
                def txt(tag):
                    node = entry.find("a:" + tag, _ARXIV_NS)
                    return (node.text or "").strip() if node is not None else ""
                url = txt("id")
                year = int(txt("published")[:4] or 0) or None
                if (y0 and year and year < y0) or (y1 and year and year > y1):
                    continue
                out.append({
                    "title": re.sub(r"\s+", " ", txt("title")),
                    "authors": ", ".join(a.find("a:name", _ARXIV_NS).text
                                         for a in entry.findall("a:author", _ARXIV_NS)
                                         if a.find("a:name", _ARXIV_NS) is not None)[:160],
                    "year": year,
                    "url": url,
                    "abstract": re.sub(r"\s+", " ", txt("summary"))[:1200],
                    "source": "arXiv",
                    "cited_by": 0,
                    "venue": "arXiv 预印本",
                    "venue_tier": "",
                })
        except Exception:
            pass
    # 按标题去重，优先 OpenAlex
    seen, unique = set(), []
    for p in out:
        key = re.sub(r"\W+", "", p["title"].lower())[:80]
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(p)
    # CCF-A / 中科院一区优先，同级按引用数降序
    unique.sort(key=lambda p: (venue_rank.rank_order(p.get("venue_tier", "")),
                               -(p.get("cited_by") or 0)))
    if cfg["prefer_top_venues"]:
        top = [p for p in unique if p.get("venue_tier")]
        if len(top) >= limit:
            return top[:limit]
        rest = [p for p in unique if not p.get("venue_tier")]
        return (top + rest)[:limit]
    return unique[:limit]


def _invert_abstract(inv):
    if not isinstance(inv, dict):
        return ""
    words = []
    for word, positions in inv.items():
        for pos in positions:
            words.append((pos, word))
    words.sort()
    return " ".join(w for _, w in words)


def search_dataset_links(name, limit=None):
    """给数据集名找公开链接：[{name, url, url_type, license, source}]。"""
    cfg = _cfg()
    limit = int(limit or cfg["max_links"])
    if not cfg["enabled"] or not (name or "").strip():
        return []
    out = []
    if "huggingface" in cfg["sources"]:
        try:
            r = _get("https://huggingface.co/api/datasets",
                     {"search": name, "limit": limit, "full": "false"},
                     cfg["timeout"])
            for d in (r.json() if isinstance(r.json(), list) else [])[:limit]:
                did = str(d.get("id") or "")
                if not did:
                    continue
                # 再取一次详情，拿到可直接下载的文件链接
                downloads = []
                try:
                    dr = _get("https://huggingface.co/api/datasets/" + did, {}, cfg["timeout"])
                    preferred = (".parquet", ".zip", ".tar", ".gz", ".json", ".csv",
                                 ".arrow", ".pkl", ".h5")
                    skip = (".gitattributes", ".gitignore", "readme", "classes.py")
                    siblings = sorted(
                        dr.json().get("siblings") or [],
                        key=lambda s: 0 if str(s.get("rfilename", "")).lower().endswith(preferred) else 1)
                    for s in siblings[:8]:
                        fname = str(s.get("rfilename") or "")
                        low = fname.lower()
                        if not fname or any(low.startswith(k) or low.endswith(k) for k in skip):
                            continue
                        downloads.append({
                            "url": f"https://huggingface.co/datasets/{did}/resolve/main/{fname}",
                            "label": fname[:60],
                        })
                        if len(downloads) >= 3:
                            break
                except Exception:
                    pass
                out.append({
                    "name": did.split("/")[-1],
                    "url": "https://huggingface.co/datasets/" + did,
                    "url_type": "huggingface",
                    "license": ((d.get("cardData") or {}).get("license") or "未知/需人工确认"),
                    "source": "Hugging Face",
                    "downloads": downloads,
                })
        except Exception:
            pass
    if len(out) < limit and "zenodo" in cfg["sources"]:
        try:
            r = _get("https://zenodo.org/api/records",
                     {"q": f'"{name}"', "size": limit}, cfg["timeout"])
            for hit in ((r.json().get("hits") or {}).get("hits") or [])[:limit]:
                md = hit.get("metadata") or {}
                links = hit.get("links") or {}
                doi_url = str(links.get("self_doi") or links.get("html") or "")
                if not doi_url.startswith("http"):
                    continue
                downloads = []
                for f in (hit.get("files") or [])[:3]:
                    u = str((f.get("links") or {}).get("self") or "")
                    if u.startswith("http"):
                        downloads.append({"url": u, "label": str(f.get("key") or "file")[:60]})
                out.append({
                    "name": str(md.get("title") or name)[:120],
                    "url": doi_url,
                    "url_type": "zenodo",
                    "license": str((md.get("license") or {}).get("id") or "未知/需人工确认"),
                    "source": "Zenodo",
                    "downloads": downloads,
                })
        except Exception:
            pass
    seen, unique = set(), []
    for item in out:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique[:limit]
