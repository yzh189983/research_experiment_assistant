"""LLM 调用层 —— 任意 OpenAI 兼容接口（智谱 / DeepSeek / Qwen / OpenAI）。

可靠性：指数退避重试（429/5xx/超时）、多模型 fallback、流式输出、
连续失败熔断、JSON 解析失败自修复一次。
"""
import json
import random
import re
import threading
import time

import requests

import config
from core import crypto

_local = threading.local()
_circuit = {"fails": 0, "open_until": 0.0}
_circuit_lock = threading.Lock()


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


def _circuit_check():
    """熔断：连续失败达到阈值时短暂拒绝调用，避免烧 token。"""
    with _circuit_lock:
        if _circuit["open_until"] > time.time():
            left = int(_circuit["open_until"] - time.time())
            raise LLMError(f"LLM 连续失败过多，已熔断保护，{left} 秒后重试")


def _circuit_fail():
    cfg = _cfg()
    threshold = int(cfg.get("circuit_break_after") or 0)
    with _circuit_lock:
        if not threshold:
            return
        _circuit["fails"] += 1
        if _circuit["fails"] >= threshold:
            _circuit["open_until"] = time.time() + 60
            _circuit["fails"] = 0


def _circuit_ok():
    with _circuit_lock:
        _circuit["fails"] = 0
        _circuit["open_until"] = 0.0


def circuit_status():
    with _circuit_lock:
        left = max(0, int(_circuit["open_until"] - time.time()))
        return {"open": left > 0, "retry_after_sec": left, "consecutive_fails": _circuit["fails"]}


def reset_circuit():
    with _circuit_lock:
        _circuit["fails"] = 0
        _circuit["open_until"] = 0.0


def _record_usage(data, r, model):
    usage = data.get("usage") or {}
    _local.last_usage = {
        "model": data.get("model") or model or "",
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or
                            (int(usage.get("prompt_tokens") or 0) +
                             int(usage.get("completion_tokens") or 0))),
        "request_id": r.headers.get("x-request-id") or data.get("id") or "",
    }


def _post_once(url, headers, payload, timeout, stream=False):
    """单次 POST；返回 (response, error_str)。429/5xx/超时 视为可重试。"""
    try:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=timeout, stream=stream)
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException as e:
        msg = crypto.redact(str(e))
        # 网络类错误可重试，其它（DNS/证书等）也可，统一重试由次数兜底
        return None, msg
    if r.status_code == 429:
        return r, "rate_limit"
    if r.status_code >= 500:
        return r, f"server_{r.status_code}"
    if r.status_code >= 400:
        return r, None  # 4xx（非 429）不可重试，交给调用方解析错误
    return r, None


def _retry_delay(attempt, base):
    return base * (2 ** attempt) + random.uniform(0, 0.3)


def _models(c):
    models = [c.get("model")]
    for m in (c.get("fallback_models") or []):
        if m and m not in models:
            models.append(m)
    return models


def chat(messages, system=None, json_mode=False, temperature=None, max_tokens=8000,
         stream=False, on_delta=None, cfg_override=None):
    """调用 LLM。支持重试/fallback/流式。

    cfg_override 可临时覆盖 llm 配置（如卡卡专用模型），不改动用户设置。
    stream=True 时逐块把 delta 交给 on_delta(text) 回调，返回完整文本。
    """
    c = _cfg()
    if cfg_override:
        c = dict(c, **{k: v for k, v in cfg_override.items() if v not in (None, "")})
    if not c.get("api_key"):
        raise LLMError("还没有配置 LLM API Key。请到「设置」填写 base_url / api_key / model。")
    if stream and on_delta is None:
        stream = False  # 无回调时退回非流式

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)

    base_payload = {
        "messages": msgs,
        "temperature": c.get("temperature", 0.35) if temperature is None else temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        base_payload["response_format"] = {"type": "json_object"}

    url = c["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {c['api_key']}",
        "Content-Type": "application/json",
    }
    timeout = c.get("timeout", 240)
    max_retries = int(c.get("max_retries", 3) or 0)
    base_delay = float(c.get("retry_base_sec", 1.0) or 1.0)
    use_stream = bool(stream or c.get("stream"))

    last_err = "未知错误"
    models = _models(c)
    for model in models:
        _circuit_check()
        for attempt in range(max_retries + 1):
            payload = dict(base_payload, model=model, stream=use_stream)
            r, err = _post_once(url, headers, payload, timeout, stream=use_stream)

            # 部分模型不支持 response_format，去掉重试一次
            if (r is not None and r.status_code >= 400 and
                    "response_format" in (r.text or "") and "response_format" in payload):
                payload.pop("response_format", None)
                r, err = _post_once(url, headers, payload, timeout, stream=use_stream)

            if err:  # 可重试错误
                last_err = err
                if attempt < max_retries:
                    time.sleep(_retry_delay(attempt, base_delay))
                    continue
                break  # 该模型重试耗尽，换下一个

            if r is None:
                last_err = "无响应"
                break

            if r.status_code >= 400:
                last_err = crypto.redact(f"LLM 返回 {r.status_code}: {(r.text or '')[:300]}")
                break  # 4xx 换模型无意义但可试下一个

            try:
                if use_stream:
                    full, usage, req_id = _parse_stream(r, on_delta, model)
                else:
                    data = r.json()
                    full = data["choices"][0]["message"]["content"]
                    usage = data.get("usage") or {}
                    req_id = r.headers.get("x-request-id") or data.get("id") or ""
                _local.last_usage = {
                    "model": model,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or
                                        (int(usage.get("prompt_tokens") or 0) +
                                         int(usage.get("completion_tokens") or 0))),
                    "request_id": req_id or "",
                }
                _circuit_ok()
                return full
            except Exception as e:
                last_err = crypto.redact(f"无法解析 LLM 响应：{e}")
                break

        _circuit_fail()
        # 主模型失败，尝试下一个 fallback
    raise LLMError(last_err)


def _parse_stream(resp, on_delta, model):
    """解析 SSE 流式响应，逐块回调 delta，返回完整文本。"""
    full = []
    usage = {}
    req_id = resp.headers.get("x-request-id") or ""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except Exception:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        try:
            delta = chunk["choices"][0].get("delta", {}).get("content") or ""
        except (KeyError, IndexError):
            delta = ""
        if delta:
            full.append(delta)
            if on_delta:
                on_delta(delta)
    return "".join(full), usage, req_id


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
    """调用 LLM 并解析 JSON；解析失败时把错误+原始输出回喂模型自修一次。"""
    raw = chat(messages, system=system, json_mode=True, **kw)
    try:
        return extract_json(raw)
    except LLMError as first_err:
        repair_msgs = list(messages) + [
            {"role": "assistant", "content": raw[:4000]},
            {"role": "user", "content":
             ("上一次响应不是合法 JSON，解析错误：%s\n"
              "请只输出修正后的合法 JSON 对象，不要解释、不要代码块。" % str(first_err)[:300])},
        ]
        try:
            fixed = chat(repair_msgs, system=system, json_mode=True, **kw)
            return extract_json(fixed)
        except Exception:
            raise first_err
