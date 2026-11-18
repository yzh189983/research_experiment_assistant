"""事件总线：SSE 实时推送日志 / 指标 / 状态变更。

每个事件既进内存队列（低延迟推给当前订阅者），也落库到 stream_events（重启 /
断线后按 id 补发）。内存 buffer 只作为热缓存，不再是唯一真相。
"""
import json
import queue
import threading

from core import db

_lock = threading.Lock()
_subscribers = {}          # exp_id -> set(queue)
_buffer = {}               # exp_id -> list[item]  最近事件，用于新连接即时补齐
BUF_MAX = 300
_last_id = 0               # 最近一次发布事件的全局 id（用于内存事件的 id 字段）


def subscribe(exp_id):
    q = queue.Queue(maxsize=2000)
    with _lock:
        _subscribers.setdefault(exp_id, set()).add(q)
        hist = list(_buffer.get(exp_id, []))
    for h in hist:
        try:
            q.put_nowait(h)
        except queue.Full:
            break
    return q


def unsubscribe(exp_id, q):
    with _lock:
        s = _subscribers.get(exp_id)
        if s:
            s.discard(q)


def publish(exp_id, item):
    """发布事件：先落库拿全局 id，再推内存订阅者。"""
    global _last_id
    if exp_id is None:
        return
    # 落库失败不应阻断实时推送，但要告警
    try:
        event_id = db.add_stream_event(exp_id, item.get("type", "note"), item,
                                       run_id=item.get("run_id"))
        _last_id = max(_last_id, event_id or 0)
        wire = dict(item)
        wire["_id"] = event_id
    except Exception as e:
        wire = dict(item)
        wire["_id"] = _last_id
        try:
            print(f"[bus] 事件落库失败: {e}", flush=True)
        except Exception:
            pass
    with _lock:
        buf = _buffer.setdefault(exp_id, [])
        buf.append(wire)
        if len(buf) > BUF_MAX:
            del buf[:len(buf) - BUF_MAX]
        subs = list(_subscribers.get(exp_id, ()))
    for q in subs:
        try:
            q.put_nowait(wire)
        except queue.Full:
            pass


def log(exp_id, line, run_id=None):
    publish(exp_id, {"type": "log", "line": line, "run_id": run_id})


def status(exp_id, st, run_id=None, extra=None):
    item = {"type": "status", "status": st, "run_id": run_id}
    if extra:
        item.update(extra)
    publish(exp_id, item)


def metric(exp_id, name, value, step=None, run_id=None):
    publish(exp_id, {"type": "metric", "name": name, "value": value, "step": step, "run_id": run_id})


def note(exp_id, message, level="info", run_id=None):
    publish(exp_id, {"type": "note", "message": message, "level": level, "run_id": run_id})


def gpu(exp_id, gpus, run_id=None):
    publish(exp_id, {"type": "gpu", "gpus": gpus, "run_id": run_id})


def summary(exp_id, data, run_id=None):
    publish(exp_id, {"type": "summary", "data": data, "run_id": run_id})
