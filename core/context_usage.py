"""卡卡上下文用量：估算当前会话占了多少 token，并换算成窗口占比。

真实用量优先取 LLM 返回的 prompt_tokens；规则模式或未调用模型时用文本估算。
估算只是粗算（英文约 4 字符/token，中文约 1.3 token/字），会在界面上标明。
"""
import json

import config
from core import db

# 系统提示 + 技能注入的保守估算，避免显示 0
SYSTEM_ESTIMATE = 1200


def estimate_tokens(text):
    if not text:
        return 0
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    if not s:
        return 0
    ascii_n = sum(1 for ch in s if ord(ch) < 128)
    return int(ascii_n / 4 + (len(s) - ascii_n) * 1.3)


def context_window(model=None):
    """窗口上限：用户配置优先，其次按模型名推断，默认 128K。"""
    cfg = config.load_config()
    k = cfg.get("kaka") or {}
    explicit = int(k.get("context_window") or 0)
    if explicit >= 1000:
        return explicit
    name = str(model or k.get("model") or (cfg.get("llm") or {}).get("model") or "").lower()
    for tag, size in (("200k", 200000), ("128k", 128000), ("64k", 64000),
                      ("32k", 32000), ("16k", 16000), ("8k", 8000)):
        if tag in name:
            return size
    if "claude" in name:
        return 200000
    return 128000


def _k(n):
    n = int(n)
    if n < 1000:
        return str(n)
    v = n / 1000.0
    return (str(int(v)) if v >= 10 else ("%.1f" % v).rstrip("0").rstrip(".")) + "K"


def _shape(used, limit, model, source):
    used, limit = max(0, int(used)), max(1, int(limit))
    return {
        "used": used,
        "limit": limit,
        "percent": round(used * 100.0 / limit, 1),
        "label": _k(used) + " / " + _k(limit),
        "model": model or "",
        "source": source,
    }


def current_usage(uid, scope="global", conversation_id=None, model=None):
    """按当前会话（或 scope 全部会话）历史估算已占用的上下文。"""
    cfg = config.load_config()
    k = cfg.get("kaka") or {}
    model = model or k.get("model") or (cfg.get("llm") or {}).get("model") or ""
    limit = context_window(model)
    used = SYSTEM_ESTIMATE
    if conversation_id:
        conv_ids = [int(conversation_id)]
    else:
        conv_ids = [c["id"] for c in db.list_scope_conversations(str(scope or "global"), uid=uid)]
    msgs = []
    for cid in conv_ids:
        msgs.extend(db.list_kaka_messages(cid, uid=uid, limit=20))
    msgs.sort(key=lambda m: m.get("id", 0))
    msgs = msgs[-20:]
    for m in msgs:
        used += estimate_tokens(m.get("content") or "")
    if msgs:
        ctx = msgs[-1].get("context_json")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx or "{}")
            except (TypeError, ValueError):
                ctx = {}
        if isinstance(ctx, dict):
            used += estimate_tokens(ctx.get("context") or "")
    return _shape(used, limit, model, "estimate")


def from_api(usage_data, model=None):
    """用模型返回的真实 prompt_tokens 生成展示数据；没有用量时返回 None。"""
    used = int((usage_data or {}).get("prompt_tokens") or 0)
    if not used:
        return None
    return _shape(used, context_window(model), model, "api")
