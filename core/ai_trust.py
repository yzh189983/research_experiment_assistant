"""AI 输出可信分级与证据绑定（产品建议3 P0-36）。

设计目标：给方案/代码/总结/审稿/诊断等 AI 输出统一打上来源层级，
并把可追溯证据（metric/log/commit/诊断行）保存到 ai_outputs 表。
"""
import json
import re

from core import db

TIERS = {
    "verified": {"label": "实测支持", "rank": 4, "color": "ok"},
    "supported": {"label": "日志/代码支持", "rank": 3, "color": "ok"},
    "inferred": {"label": "AI 推断", "rank": 2, "color": "run"},
    "external": {"label": "外部知识", "rank": 1, "color": "idle"},
    "unverified": {"label": "待验证", "rank": 0, "color": "warn"},
}

_INFER_RE = re.compile(r"(建议|可能|或许|推测|可以|考虑|倾向于|大概率|应该|需要进一步|值得)", re.I)
_EXTERNAL_RE = re.compile(r"(文献|论文|已有研究|通常|一般|业界|常见做法|据报道)", re.I)


def tier_info(tier):
    return TIERS.get(tier or "unverified", TIERS["unverified"])


def classify(text, evidence=None):
    """对单条 AI 文本做保守分级：有实测证据优先，推断词降级。"""
    evidence = evidence or {}
    text = str(text or "")
    if evidence.get("metric") or evidence.get("metrics") or evidence.get("sources"):
        if not _INFER_RE.search(text):
            return "verified"
    if evidence.get("line_no") or evidence.get("commit") or evidence.get("log_line"):
        return "supported"
    if _INFER_RE.search(text):
        return "inferred"
    if _EXTERNAL_RE.search(text):
        return "external"
    return "unverified"


def split_claims(text, limit=8):
    """把长文本拆成短句，逐句分级。"""
    text = str(text or "")
    parts = [p.strip() for p in re.split(r"(?<=[。！？；\n])", text) if p.strip()]
    if not parts:
        parts = [text[:240]] if text else []
    return [{"text": p[:240], "tier": classify(p)} for p in parts[:limit]]


def overall_tier(claims):
    """整体采用最保守层级，避免用一条实测数据包装整段推断。"""
    if not claims:
        return "unverified"
    return min((c.get("tier") or "unverified" for c in claims),
               key=lambda t: tier_info(t)["rank"])


def build_evidence_from_summary(summary, run=None, experiment=None):
    verification = (summary or {}).get("verification") or {}
    run = run or {}
    experiment = experiment or {}
    diagnosis = {}
    try:
        diagnosis = json.loads(run.get("diagnosis_json") or "{}")
    except Exception:
        pass
    diag_evidence = diagnosis.get("evidence") or {}
    return {
        "metrics": verification.get("observed_claims") or [],
        "sources": verification.get("evidence_sources") or [],
        "unverified_numbers": verification.get("unverified_numbers") or [],
        "criteria": verification.get("criteria") or {},
        "commit": run.get("git_sha") or "",
        "line_no": diag_evidence.get("line_no") or 0,
        "log_line": diag_evidence.get("line") or "",
        "experiment_id": experiment.get("id") or 0,
        "run_id": run.get("id") or 0,
    }


def _to_text(content):
    if isinstance(content, str):
        return content
    return json.dumps(content or {}, ensure_ascii=False)


def _brief(content):
    if isinstance(content, dict):
        for key in ("one_liner", "verdict", "signature", "cause", "summary", "title"):
            if content.get(key):
                return str(content[key])[:300]
        issues = content.get("major_issues") or content.get("fixes") or []
        if issues:
            return str(issues[0])[:300]
    return _to_text(content)[:300]


def record_output(experiment_id, run_id, kind, content, evidence=None, uid=None):
    """保存一条带可信分级的 AI 输出；失败不影响主流程。"""
    try:
        text = _to_text(content)
        claims = split_claims(text)
        tier = overall_tier(claims)
        if (evidence or {}).get("unverified_numbers") and tier == "verified":
            tier = "inferred"
        return db.add_ai_output(
            experiment_id=experiment_id, run_id=run_id, kind=kind, tier=tier,
            summary=_brief(content),
            content={"claims": claims, "notice": "AI 生成，需人工判断。"},
            evidence=evidence or {}, uid=uid)
    except Exception:
        return None
