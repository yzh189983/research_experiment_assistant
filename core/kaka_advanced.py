"""卡卡高级能力：55 技能配置 / 56 RSI 自进化 / 57 上下文可视化 / 58 SuperAgent 会话路由。

设计原则（与 AI P0 一致）：
- 技能只影响提示词与参数，不自行改代码 / 判据 / 报告。
- RSI 默认关闭，只产出提案，须人工批准；不绕过权限与可信分级。
- 上下文快照只记录"卡卡看到什么"，不写入未经核实的结论。
- 话题路由只在同用户、同权限 scope 内进行。
"""
import json
import re
import time

from core import db

# ---------------- 55 卡卡技能 ----------------
# 官方技能市场条目：内置定义。每项是一段可注入 system 的行为说明 + 参数 schema。
# 字段：key / name / desc / category / prompt（注入卡卡 system）/ params（默认值+说明）/ tier
SKILL_MARKET = [
    {
        "key": "metric_watch", "name": "指标异常监测", "category": "监控",
        "desc": "盯指标曲线，连续多步不增长时用自然语言提示，而不只看数字。",
        "prompt": "当验证指标连续多步无增长时，用自然语言说明趋势并给出是否继续观察的建议。",
        "params": {"patience": {"default": 6, "desc": "连续多少步无增长才提示"}},
        "tier": "verified",
    },
    {
        "key": "anomaly_explain", "name": "异常诊断解读", "category": "监控",
        "desc": "运行失败或日志出现 NaN/OOM 时，先给证据行号再给修复建议。",
        "prompt": "发现失败或异常时，先引用日志证据行，再给出可执行的修复建议，不臆测。",
        "params": {"max_suggestions": {"default": 3, "desc": "最多给几条修复建议"}},
        "tier": "verified",
    },
    {
        "key": "hypothesis_review", "name": "假设审视", "category": "实验设计",
        "desc": "写方案时主动检查假设是否可证伪、是否有对照。",
        "prompt": "审视研究假设是否可证伪、是否设置基线与消融，缺失时明确指出。",
        "params": {"strict": {"default": True, "desc": "严格模式：缺对照即判为证据不足"}},
        "tier": "inferred",
    },
    {
        "key": "paper_polish", "name": "论文段落润色", "category": "写作",
        "desc": "总结时输出可直接进论文 Results 的客观段落，带关键数值。",
        "prompt": "写论文结果段落时只陈述实测数值，客观中性的语气，不夸大结论。",
        "params": {"max_chars": {"default": 250, "desc": "段落最多字数"}},
        "tier": "verified",
    },
    {
        "key": "dataset_curator", "name": "数据集管家", "category": "资料",
        "desc": "找数据集时优先官方来源，标注许可与年份范围。",
        "prompt": "整理数据集时优先官方来源，标注许可、年份与下载链接，不确定项标需人工确认。",
        "params": {"prefer_ccfa": {"default": True, "desc": "优先 CCF-A / 中科院一区论文"}},
        "tier": "supported",
    },
    {
        "key": "criteria_guard", "name": "判据守门员", "category": "规范",
        "desc": "在结论前检查判据是否已锁定，未锁定则提醒不能自动判通过。",
        "prompt": "给出结论前先确认预注册判据是否锁定；未锁定时提醒用户，不自动判定通过。",
        "params": {},
        "tier": "verified",
    },
    {
        "key": "concise_mode", "name": "简洁回答", "category": "写作",
        "desc": "让卡卡回答更短，直奔结论与证据。",
        "prompt": "回答保持简洁，2~4 句，先给结论再给证据，不写 Markdown 标题。",
        "params": {"max_sentences": {"default": 4, "desc": "最多几句"}},
        "tier": "inferred",
    },
]

_MARKET_BY_KEY = {s["key"]: s for s in SKILL_MARKET}


def market_list(uid=None):
    """官方技能市场清单（含是否已安装/启用）。"""
    installed = {}
    for s in db.list_kaka_skills(uid=uid):
        cfg = _j(s.get("config_json"), {})
        installed[s["skill_key"]] = {"enabled": s["enabled"], "config": cfg}
    out = []
    for s in SKILL_MARKET:
        row = dict(s)
        row["installed"] = s["key"] in installed
        row["enabled"] = bool(installed[s["key"]]["enabled"]) if s["key"] in installed else False
        row["user_config"] = installed[s["key"]]["config"] if s["key"] in installed else {}
        row["source"] = "builtin"
        out.append(row)
    # 追加从 ClawHub 导入的技能（统一展示，便于开关/卸载）
    for row in db.list_kaka_skills(uid=uid):
        src = str(row.get("source") or "")
        if not src.startswith("clawhub:"):
            continue
        out.append({
            "key": row["skill_key"], "name": row.get("name") or row["skill_key"],
            "category": "ClawHub", "desc": (row.get("description") or "")[:300],
            "prompt": row.get("prompt_text") or "", "params": {}, "tier": "supported",
            "installed": True, "enabled": bool(row["enabled"]), "user_config": {},
            "source": "clawhub", "version": row.get("version") or "",
            "source_url": row.get("source_url") or "",
        })
    return out


def install_skill(skill_key, config=None, uid=None):
    """从官方市场安装/更新一个技能。未知 key 拒绝。"""
    skill = _MARKET_BY_KEY.get(str(skill_key))
    if not skill:
        return {"error": "技能市场里没有这个技能"}
    cfg = _merge_defaults(skill, config or {})
    db.upsert_kaka_skill(skill["key"], enabled=True, config=cfg,
                         source="market:official", uid=uid)
    return {"ok": True, "skill": skill["key"], "config": cfg}


def uninstall_skill(skill_key, uid=None):
    db.delete_kaka_skill(skill_key, uid=uid)
    return {"ok": True}


def set_skill(skill_key, enabled=None, config=None, uid=None):
    """开关 / 改参数；内置市场技能与已安装的 ClawHub 技能都支持。"""
    cur = db.get_kaka_skill(skill_key, uid=uid)
    skill = _MARKET_BY_KEY.get(str(skill_key))
    is_hub = bool(cur and str(cur.get("source") or "").startswith("clawhub:"))
    if not skill and not is_hub:
        return {"error": "未知技能"}
    if skill:
        cfg = _merge_defaults(skill, config if config is not None else (cur["config"] if cur else {}))
    else:
        cfg = config if config is not None else (cur["config"] if cur else {})
    en = bool(enabled) if enabled is not None else (bool(cur["enabled"]) if cur else True)
    if is_hub:
        db.upsert_kaka_skill(skill_key, enabled=en, config=cfg, source=cur["source"],
                             name=cur.get("name") or "", description=cur.get("description") or "",
                             prompt_text=cur.get("prompt_text") or "", version=cur.get("version") or "",
                             source_url=cur.get("source_url") or "", uid=uid)
    else:
        db.upsert_kaka_skill(skill_key, enabled=en, config=cfg,
                             source=cur["source"] if cur else "builtin", uid=uid)
    return {"ok": True, "skill": skill_key, "enabled": en, "config": cfg}


def installed_skills(uid=None):
    """已启用技能，返回注入用的 prompt 与配置（含 ClawHub 导入技能）。"""
    out = []
    for row in db.list_kaka_skills(uid=uid):
        if not row["enabled"]:
            continue
        skill = _MARKET_BY_KEY.get(row["skill_key"])
        if skill:
            cfg = _merge_defaults(skill, _j(row.get("config_json"), {}))
            out.append({"key": skill["key"], "name": skill["name"],
                        "prompt": skill["prompt"], "config": cfg, "tier": skill["tier"],
                        "source": "builtin"})
        elif str(row.get("source") or "").startswith("clawhub:"):
            prompt = (row.get("prompt_text") or row.get("description") or "").strip()
            if prompt:
                out.append({"key": row["skill_key"], "name": row.get("name") or row["skill_key"],
                            "prompt": prompt[:1500], "config": _j(row.get("config_json"), {}),
                            "tier": "supported", "source": "clawhub"})
    return out


def skills_prompt(uid=None, selected_keys=None):
    """把技能拼成 system 注入文本。
    selected_keys=None 表示全部已启用（默认兼容）；列表表示仅注入这些 key。
    只从已启用集合里选，避免注入未启用的技能。
    """
    skills = installed_skills(uid=uid)
    if selected_keys is not None:
        wanted = set(str(k) for k in selected_keys)
        skills = [s for s in skills if s["key"] in wanted]
    if not skills:
        return ""
    lines = ["【本次对话使用的卡卡技能，回答时遵守】"]
    for s in skills:
        lines.append(f"- {s['name']}：{s['prompt']}")
    return "\n".join(lines)


def _merge_defaults(skill, user_cfg):
    out = {}
    for k, spec in (skill.get("params") or {}).items():
        out[k] = spec.get("default")
    if isinstance(user_cfg, dict):
        for k, v in user_cfg.items():
            if k in out:
                out[k] = v
    return out


def _j(value, default):
    if isinstance(value, default.__class__):
        return value
    try:
        return json.loads(value or "") if default is not None else default
    except Exception:
        return default


# ---------------- 56 RSI 自进化 ----------------
RSI_CONFIG_DEFAULT = {"enabled": False, "window": 50, "min_samples": 5}


def rsi_config(uid=None):
    cfg = db.get_profile(uid).get("rsi") or {}
    out = dict(RSI_CONFIG_DEFAULT)
    if isinstance(cfg, dict):
        out.update({k: cfg[k] for k in RSI_CONFIG_DEFAULT if k in cfg})
    return out


def set_rsi_config(patch, uid=None):
    cfg = rsi_config(uid=uid)
    for k in RSI_CONFIG_DEFAULT:
        if isinstance(patch, dict) and k in patch:
            cfg[k] = patch[k]
    cfg["enabled"] = bool(cfg["enabled"])
    profile = db.get_profile(uid)
    profile["rsi"] = cfg
    db.save_profile(uid, profile)
    return cfg


def record_experience(behavior, context="", feedback="", outcome="", uid=None):
    """记录一次卡卡行为，供 RSI 提炼。只在 RSI 配置存在时记录，避免无谓写入。"""
    try:
        return db.add_rsi_experience(behavior, context, feedback, outcome, uid=uid)
    except Exception:
        return None


def generate_proposals(uid=None):
    """从经验里提炼改进提案（规则版）：统计反馈分布，产出可审核的提案。
    不自动生效，须人工批准。"""
    cfg = rsi_config(uid=uid)
    exps = db.list_rsi_experiences(uid=uid, limit=int(cfg.get("window") or 50))
    if len(exps) < int(cfg.get("min_samples") or 5):
        return {"error": f"样本不足（{len(exps)}/{cfg.get('min_samples')}），暂不生成提案",
                "count": 0}
    feedback_counts = {}
    behavior_counts = {}
    for e in exps:
        fb = (e.get("feedback") or "none").lower()
        behavior_counts[e.get("behavior") or "other"] = behavior_counts.get(e.get("behavior") or "other", 0) + 1
        feedback_counts[fb] = feedback_counts.get(fb, 0) + 1
    made = []
    useful = feedback_counts.get("useful", 0)
    if useful >= 2:
        pid = db.add_rsi_proposal(
            category="prompt", title="保留当前回答风格",
            target_key="kaka.system",
            rationale=f"最近 {len(exps)} 次行为中 {useful} 次被标记有用，建议保持当前措辞与证据组织方式。",
            old_value="按默认提示词回答",
            new_value="保持现有证据先行、结论随后的组织方式",
            evidence=[{"feedback_counts": feedback_counts}], uid=uid)
        made.append(pid)
    corrected = feedback_counts.get("corrected", 0) + feedback_counts.get("ignored", 0)
    if corrected >= 2:
        pid = db.add_rsi_proposal(
            category="prompt", title="减少无证据断言",
            target_key="kaka.system",
            rationale=f"最近 {len(exps)} 次行为中 {corrected} 次被纠正或忽略，建议回答更保守。",
            old_value="直接给出结论",
            new_value="证据不足时先说明不确定性，再给结论",
            evidence=[{"feedback_counts": feedback_counts, "behavior_counts": behavior_counts}],
            uid=uid)
        made.append(pid)
    if behavior_counts.get("warning", 0) >= 3:
        pid = db.add_rsi_proposal(
            category="skill_param", title="减少过早告警",
            target_key="metric_watch.patience",
            rationale=f"监测类行为 {behavior_counts['warning']} 次，建议延长观察窗口再提示。",
            old_value="6", new_value="8",
            evidence=[{"behavior_counts": behavior_counts}], uid=uid)
        made.append(pid)
    return {"ok": True, "count": len(made), "proposals": db.list_rsi_proposals(uid=uid, status="pending", limit=10)}


def list_proposals(status=None, uid=None):
    return db.list_rsi_proposals(uid=uid, status=status, limit=100)


def decide_proposal(proposal_id, decision, uid=None):
    if not db.decide_rsi_proposal(proposal_id, decision, uid=uid):
        return {"error": "提案不存在或无权操作"}
    return {"ok": True, "id": proposal_id, "status": decision}


# ---------------- 57 上下文可视化 ----------------
def build_context_snapshot(kind, eid=None, pid=None, uid=None, system_name="", context_text=""):
    """把本次回答所用的上下文整理成可展示、可回查的结构。"""
    snap = {
        "scope_kind": kind,
        "system": system_name or "",
        "context": context_text or "",
        "experiment": None, "project": None,
        "metrics": [], "log_lines": [], "recent_messages": [],
        "evidence": {},
        "generated_at": time.time(),
    }
    try:
        if kind == "exp" and eid:
            exp = db.get_experiment(eid, uid=uid)
            if exp:
                snap["experiment"] = {
                    "id": eid, "title": exp.get("title"),
                    "status": exp.get("status"),
                    "innovation": (exp.get("innovation") or "")[:300],
                }
            series = db.metrics_series(db.latest_run(eid, uid=uid)["id"], limit=6) if db.latest_run(eid, uid=uid) else {}
            for name, pts in series.items():
                snap["metrics"].append({"name": name, "last": pts[-1]["v"] if pts else None,
                                        "points": len(pts)})
            tail = (db.latest_run(eid, uid=uid) or {}).get("log_tail") or ""
            snap["log_lines"] = [ln for ln in tail.splitlines() if ln.strip()][-6:]
        elif kind == "project" and pid:
            p = db.get_project(pid)
            if p:
                snap["project"] = {"id": pid, "name": p.get("name")}
    except Exception:
        pass
    return snap


# ---------------- 58 SuperAgent：话题路由 ----------------
_STOPWORDS = set("的 了 和 是 在 有 我 你 他 它 这 那 把 给 为 与 及 或 一个 什么 怎么 为什么 请问 帮我 一下 可以 吗 呢 吧".split())


def topic_keywords(text):
    """从问题里抽取话题关键词：英文整词 + 中文 2 字词（过滤停用词与碎片）。"""
    text = str(text or "")
    en = set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}", text.lower()))
    zh = set()
    junk = set("的是有了和与及或把我你他它这那为给在从对将就都也很更最怎样个")
    for seg in re.split(r"[^\u4e00-\u9fa5]+", text):
        for i in range(len(seg) - 1):
            tok = seg[i:i + 2]
            if tok in _STOPWORDS:
                continue
            # 首尾含虚词的片段（如"的指""标曲"）多为跨词碎片，丢弃
            if tok[0] in junk or tok[1] in junk:
                continue
            zh.add(tok)
    return sorted(en)[:12] + sorted(zh)[:12]


def route_conversation(question, scope, uid=None, days=7):
    """话题路由：相同话题续接最近会话，不同话题新开。
    只在同用户、同 scope 内比对，杜绝跨权限串联。"""
    convs = db.list_kaka_conversations(uid=uid, days=days, limit=50)
    convs = [c for c in convs if c.get("scope") == scope]
    kws = topic_keywords(question)
    kwset = set(kws)
    best, best_score = None, 0.0
    for c in convs:
        existing = set((c.get("keywords") or "").split(",")) - {""}
        if not existing:
            continue
        inter = kwset & existing
        score = len(inter) / max(1, min(len(kwset), len(existing)))
        if score > best_score:
            best, best_score = c, score
    title = _make_title(question)
    if best and best_score >= 0.4:
        # 续接：补充关键词，刷新活跃时间
        merged = list(set((best.get("keywords") or "").split(",") + kws) - {""})[:20]
        db.update_kaka_conversation(best["id"], uid=uid, keywords=",".join(merged))
        return {"conversation_id": best["id"], "topic": best.get("topic") or title,
                "routed": "existing", "score": round(best_score, 2)}
    cid = db.create_kaka_conversation(scope, uid=uid, topic=title, keywords=",".join(kws[:20]))
    return {"conversation_id": cid, "topic": title, "routed": "new", "score": 0.0}


def _make_title(question, max_len=18):
    q = re.sub(r"\s+", " ", str(question or "")).strip()
    q = re.sub(r"^(请问|帮我|我想|麻烦)", "", q)
    return q[:max_len] or "新话题"


def archive_conversation(conv_id, uid=None):
    db.update_kaka_conversation(conv_id, uid=uid, status="archived")
    return {"ok": True}


def trash_conversation(conv_id, uid=None):
    db.trash_kaka_conversation(conv_id, uid=uid)
    return {"ok": True}


def restore_conversation(conv_id, uid=None):
    db.restore_kaka_conversation(conv_id, uid=uid)
    return {"ok": True}


def delete_conversation(conv_id, uid=None):
    if not db.delete_kaka_conversation(conv_id, uid=uid):
        return {"ok": False, "error": "会话不存在或无权限"}
    return {"ok": True}


def recent_conversations(uid=None, days=7, limit=50, status=None):
    convs = db.list_kaka_conversations(uid=uid, days=days, limit=limit, status=status)
    out = []
    for c in convs:
        last = db.list_kaka_messages(c["id"], uid=uid, limit=1)
        out.append({
            "id": c["id"], "scope": c.get("scope"), "topic": c.get("topic") or "未命名话题",
            "keywords": [k for k in (c.get("keywords") or "").split(",") if k][:8],
            "status": c.get("status") or "active",
            "updated_at": c.get("last_active_at") or c.get("updated_at"),
            "preview": (last[0]["content"][:80] if last else ""),
        })
    return out
