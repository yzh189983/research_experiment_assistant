"""ClawHub（OpenClaw 官方技能市场）接入：搜索 / 浏览 / 下载为卡卡技能。

安全边界（遵循 AI P0）：
- 只读取 SKILL.md 的 name/description 与 Markdown 正文，作为"行为提示词"注入卡卡。
- 不执行 SKILL.md/frontmatter 里的任何安装命令、脚本、二进制（ignore metadata.install）。
- 下载大小与文本长度设上限；来源 URL 仅作展示，不自动访问。
- 所有导入技能标记来源 clawhub:<owner>/<slug>，可随时卸载。
"""
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from core import db

BASE = "https://clawhub.ai"
TIMEOUT = 20
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 3000
UA = {"User-Agent": "Mozilla/5.0 (research-experiment-assistant; skill-browser)"}


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Content-Type", ""), r.read(MAX_ZIP_BYTES + 1)


def _json(url):
    ct, body = _get(url)
    if "json" not in ct:
        raise RuntimeError("技能市场返回的不是 JSON")
    return json.loads(body.decode("utf-8", "ignore"))


def search(q="", sort="trending", limit=12):
    """q 非空走 /api/v1/search，否则走 /api/v1/skills 热门列表。返回精简条目。"""
    try:
        if q:
            url = BASE + "/api/v1/search?" + urllib.parse.urlencode(
                {"q": q, "limit": limit, "nonSuspiciousOnly": "true"})
            data = _json(url)
            raw = data.get("results") or []
        else:
            url = BASE + "/api/v1/skills?" + urllib.parse.urlencode(
                {"limit": limit, "sort": sort if sort in ("trending", "downloads", "stars", "updated") else "trending",
                 "nonSuspiciousOnly": "true"})
            data = _json(url)
            raw = data.get("items") or []
    except urllib.error.HTTPError as e:
        return {"error": f"技能市场请求失败（HTTP {e.code}）"}
    except Exception as e:
        return {"error": f"连接技能市场失败：{e}"}
    items = []
    for x in raw:
        inst = x.get("install") or {}
        owner = (x.get("ownerHandle") or (x.get("owner") or {}).get("handle") or "")
        slug = x.get("slug") or ""
        ref = inst.get("reference") or (f"{owner}/{slug}" if owner and slug else slug)
        version = x.get("version") or (x.get("latestVersion") or {}).get("version") or ""
        items.append({
            "ref": ref, "owner": owner, "slug": slug,
            "name": x.get("displayName") or slug,
            "summary": (x.get("summary") or "")[:300],
            "version": version,
            "downloads": x.get("downloads") or (x.get("stats") or {}).get("downloads") or 0,
            "official": bool(x.get("official")) or (x.get("source") == "official"),
            "featured": bool(x.get("featured")),
            "trust": x.get("trust") or "",
            "canonical": x.get("canonicalUrl") or "",
        })
    return {"ok": True, "items": items, "source": "ClawHub", "query": q}


def detail(owner, slug):
    """获取单个技能详情（含 moderation 状态）。"""
    if not slug:
        return {"error": "缺少 slug"}
    try:
        params = {"slug": slug}
        if owner:
            params["ownerHandle"] = owner
        d = _json(BASE + "/api/v1/skills/" + urllib.parse.quote(slug))
        skill = d.get("skill") or {}
        mod = d.get("moderation") or {}
        return {"ok": True, "name": skill.get("displayName") or slug,
                "summary": skill.get("summary") or "", "slug": slug, "owner": owner,
                "moderation": mod.get("verdict") or "", "suspicious": bool(mod.get("isSuspicious")),
                "malware_blocked": bool(mod.get("isMalwareBlocked"))}
    except urllib.error.HTTPError as e:
        return {"error": f"获取技能详情失败（HTTP {e.code}）"}
    except Exception as e:
        return {"error": f"获取技能详情失败：{e}"}


def _parse_skill_md(text):
    """解析 SKILL.md：取 frontmatter 的 name/description + 正文（去掉安装元数据）。"""
    text = text or ""
    name = description = ""
    body = text
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if fm:
        front, body = fm.group(1), fm.group(2)
        m_name = re.search(r"^name:\s*(.+)$", front, re.M)
        m_desc = re.search(r"^description:\s*['\"]?(.+?)['\"]?\s*$", front, re.M)
        if m_name:
            name = m_name.group(1).strip().strip("'\"")
        if m_desc:
            description = m_desc.group(1).strip()
    body = body.strip()
    # 截断到合理长度，避免超长技能挤占上下文
    prompt = body[:MAX_PROMPT_CHARS]
    return name, description, prompt


def _download_parts(owner, slug, version=""):
    """返回 (name, description, prompt, real_version)；仅提取文本，不执行任何东西。"""
    params = {"slug": slug}
    if owner:
        params["ownerHandle"] = owner
    if version:
        params["version"] = version
    else:
        params["tag"] = "latest"
    url = BASE + "/api/v1/download?" + urllib.parse.urlencode(params)
    ct, data = _get(url)
    if len(data) > MAX_ZIP_BYTES:
        raise RuntimeError("技能包过大，已拒绝下载")
    if "zip" in ct or data[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(data))
        cand = [n for n in zf.namelist() if n.lower().endswith("skill.md")]
        if not cand:
            raise RuntimeError("技能包里没有 SKILL.md")
        text = zf.read(cand[0]).decode("utf-8", "ignore")
        name, desc, prompt = _parse_skill_md(text)
        return name, desc, prompt, version or "latest"
    if "json" in ct:
        # GitHub 移交 JSON：不代理文件字节，提示用户去上游查看；不自动装
        info = json.loads(data.decode("utf-8", "ignore"))
        repo = info.get("repo") or ""
        raise RuntimeError(
            "该技能由 GitHub 托管（" + repo + "），未内置文件；已跳过自动导入。")
    raise RuntimeError("无法识别的技能包格式")


def install(ref, uid=None):
    """把 ClawHub 技能导入为卡卡技能（只导入说明文本）。ref 形如 owner/slug。"""
    ref = str(ref or "").strip().lstrip("/")
    owner = ""
    if ":" in ref and ref.split(":", 1)[0].islower():
        # 形如 skills-sh:openai/skills/pdf 的来源前缀
        prefix, rest = ref.split(":", 1)
        owner, slug = prefix, rest
    elif "/" in ref:
        owner, slug = ref.split("/", 1)
    else:
        owner, slug = "", ref
    slug = slug.strip("/")
    # slug 可含斜杠（GitHub 托管的技能路径），但拒绝空白与明显非法字符
    if not slug or len(slug) > 200 or re.search(r"\s", slug):
        return {"error": "非法的技能 slug"}
    # 先查详情，拦截被标记为恶意的技能
    det = detail(owner, slug)
    if det.get("malware_blocked"):
        return {"error": "该技能已被技能市场标记为恶意，拒绝导入"}
    try:
        name, desc, prompt, version = _download_parts(owner, slug)
    except urllib.error.HTTPError as e:
        hint = "；该技能可能由 GitHub 托管，未内置文件，请到 ClawHub 查看" if e.code in (404, 409) else ""
        return {"error": f"下载技能失败（HTTP {e.code}）{hint}"}
    except Exception as e:
        return {"error": str(e)}
    if not prompt:
        return {"error": "技能没有可导入的说明正文"}
    key = f"clawhub:{owner}/{slug}" if owner else f"clawhub:{slug}"
    source_url = f"{BASE}/skills/{urllib.parse.quote(owner)}/skills/{urllib.parse.quote(slug)}" if owner else f"{BASE}/skills/{urllib.parse.quote(slug)}"
    db.upsert_kaka_skill(
        key, enabled=True, config={}, source=key,
        name=name or slug, description=desc, prompt_text=prompt,
        version=version, source_url=source_url, uid=uid)
    return {"ok": True, "key": key, "name": name or slug,
            "description": desc, "version": version, "source_url": source_url}
