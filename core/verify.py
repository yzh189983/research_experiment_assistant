"""结果核验：把总结里的数字回查指标与日志，避免无来源结论。"""
import json
import re

_NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:%|[eE][-+]?\d+)?")
_OPS = {
    "gte": lambda a, b: a >= b,
    ">=": lambda a, b: a >= b,
    "gt": lambda a, b: a > b,
    ">": lambda a, b: a > b,
    "lte": lambda a, b: a <= b,
    "<=": lambda a, b: a <= b,
    "lt": lambda a, b: a < b,
    "<": lambda a, b: a < b,
    "eq": lambda a, b: abs(a - b) < 1e-9,
    "==": lambda a, b: abs(a - b) < 1e-9,
}


def _as_number(value):
    try:
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return None


def _load(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


def evidence_sources(metrics_stats, log_tail):
    lines = [ln.strip() for ln in (log_tail or "").splitlines() if ln.strip()]
    sources = []
    for stat in metrics_stats or []:
        name = stat.get("name", "")
        value = stat.get("last")
        source = {"metric": name, "value": value,
                  "step": stat.get("last_step"), "lines": []}
        for idx, line in enumerate(lines[-200:], 1):
            if name and name.lower() in line.lower():
                source["lines"].append({"line": idx, "text": line[:180]})
            if len(source["lines"]) >= 3:
                break
        if not source["lines"] and value is not None:
            source["lines"].append({"line": 0, "text": f"metrics 表：{name} last={value}"})
        sources.append(source)
    return sources


def verify_numbers(summary, metrics_stats):
    known = set()
    for stat in metrics_stats or []:
        for key in ("first", "last", "best", "worst"):
            v = _as_number(stat.get(key))
            if v is not None:
                known.add(round(v, 4))
        if stat.get("last_step") is not None:
            v = _as_number(stat.get("last_step"))
            if v is not None:
                known.add(round(v, 4))
    texts = []
    for key in ("verdict", "one_liner", "key_findings", "comparison", "paper_paragraph"):
        val = summary.get(key)
        if isinstance(val, list):
            texts.extend([str(x) for x in val])
        elif val:
            texts.append(str(val))
    unverified = []
    for text in texts:
        for raw in _NUM_RE.findall(text):
            value = _as_number(raw)
            if value is None:
                continue
            if round(value, 4) not in known and abs(value) >= 0.01:
                unverified.append({"token": raw, "reason": "未在 metrics 中找到对应数值"})
    return unverified[:30]


def check_criteria(criteria, criteria_locked, plan, metrics_stats):
    criteria = _load(criteria)
    checks = []
    groups = []
    if isinstance(criteria.get("success"), list):
        groups.append(("success", criteria["success"]))
    if isinstance(criteria.get("failure"), list):
        groups.append(("failure", criteria["failure"]))
    stats = {s.get("name"): s for s in metrics_stats or []}

    for kind, items in groups:
        for item in items:
            if not isinstance(item, dict):
                checks.append({"kind": kind, "criterion": str(item),
                               "status": "需人工确认", "reason": "判据不是结构化规则"})
                continue
            name = item.get("metric")
            op = str(item.get("op", "gte")).lower()
            threshold = _as_number(item.get("threshold"))
            stat = stats.get(name) or {}
            observed = _as_number(stat.get("last") if item.get("use") != "best" else stat.get("best"))
            if threshold is None or observed is None or op not in _OPS:
                checks.append({"kind": kind, "metric": name, "threshold": item.get("threshold"),
                               "observed": observed, "status": "待确认",
                               "reason": "缺少可自动核验的指标或阈值"})
                continue
            passed = _OPS[op](observed, threshold)
            if kind == "failure":
                passed = not passed
            checks.append({"kind": kind, "metric": name, "op": op,
                           "threshold": threshold, "observed": observed,
                           "status": "通过" if passed else "未通过",
                           "source": f"metrics:{name}@step={stat.get('last_step')}"})

    if not checks:
        plan = _load(plan)
        raw = []
        if plan.get("success_criteria"):
            raw.append(("success", str(plan["success_criteria"])))
        if plan.get("failure_criteria"):
            raw.append(("failure", str(plan["failure_criteria"])))
        for kind, text in raw:
            checks.append({"kind": kind, "criterion": text[:240],
                           "status": "待确认",
                           "reason": "判据未结构化锁定，需人工对照结果"})
    return {
        "locked": bool(criteria_locked),
        "checks": checks,
        "summary": ("已按锁定判据自动核验" if criteria_locked and checks
                    else "判据未锁定或未结构化，结论需人工确认"),
    }


def verify_summary(summary, metrics_stats, log_tail="", criteria=None,
                   criteria_locked=False, plan=None):
    unverified = verify_numbers(summary, metrics_stats)
    return {
        "evidence_sources": evidence_sources(metrics_stats, log_tail),
        "unverified_numbers": unverified,
        "criteria": check_criteria(criteria, criteria_locked, plan, metrics_stats),
        "observed_claims": [
            {"metric": s.get("name"), "value": s.get("last"), "step": s.get("last_step")}
            for s in metrics_stats or []],
        "interpretive_notice": "verdict / one_liner / paper_paragraph 为推断性解读；metrics_stats 为实测数据。",
    }
