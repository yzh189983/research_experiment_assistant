"""LLM 调用层 —— 任意 OpenAI 兼容接口（智谱 / DeepSeek / Qwen / OpenAI）。"""
import json
import re
import threading
import requests

import config
from core import crypto

_local = threading.local()


def last_usage():
    """返回当前线程最近一次 LLM 调用的 token 用量，供成本账本记录。"""
    return getattr(_local, "last_usage", None)



class LLMError(Exception):
    pass


def _cfg():
    return config.load_config()["llm"]


def is_configured():
    c = _cfg()
    return bool(c.get("api_key"))


def chat(messages, system=None, json_mode=False, temperature=None, max_tokens=8000):
    c = _cfg()
    if not c.get("api_key"):
        raise LLMError("还没有配置 LLM API Key。请到「设置」填写 base_url / api_key / model。")

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)

    payload = {
        "model": c.get("model"),
        "messages": msgs,
        "temperature": c.get("temperature", 0.35) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    url = c["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=c.get("timeout", 240))
        # 部分模型不支持 response_format，去掉重试一次
        if r.status_code >= 400 and "response_format" in r.text:
            payload.pop("response_format", None)
            r = requests.post(url, headers=headers, json=payload, timeout=c.get("timeout", 240))
    except requests.RequestException as e:
        raise LLMError(crypto.redact(f"请求 LLM 失败：{e}"))

    if r.status_code >= 400:
        raise LLMError(crypto.redact(f"LLM 返回 {r.status_code}: {r.text[:500]}"))
    try:
        data = r.json()
        usage = data.get("usage") or {}
        _local.last_usage = {
            "model": data.get("model") or c.get("model") or "",
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or
                                (int(usage.get("prompt_tokens") or 0) +
                                 int(usage.get("completion_tokens") or 0))),
            "request_id": r.headers.get("x-request-id") or data.get("id") or "",
        }
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise LLMError(crypto.redact(f"无法解析 LLM 响应：{r.text[:500]}"))


def extract_json(text):
    """从可能带 ``` 包裹或前后废话的文本里抠出 JSON 对象。"""
    if not text:
        raise LLMError("LLM 返回为空")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 找到第一个 { 到最后一个 } 的平衡片段
    start = t.find("{")
    if start == -1:
        raise LLMError(f"响应中没有 JSON：{text[:300]}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception as e:
                    raise LLMError(f"JSON 解析失败：{e}")
    raise LLMError("JSON 结构不完整")


def chat_json(messages, system=None, **kw):
    return extract_json(chat(messages, system=system, json_mode=True, **kw))
