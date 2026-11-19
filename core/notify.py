"""通知通道：把关键事件通过 Webhook 推送到企业微信 / 钉钉 / 自定义端点。

设计要点：
- 配置按用户隔离（config.load_config(uid)），从 experiment_id 反查 uid。
- 后台线程调用，绝不抛异常到调用方；失败只记日志。
- 事件类型白名单 + 免打扰时段，避免打扰。
"""
import datetime
import json
import logging
import threading

import requests

import config
from core import crypto, db

log = logging.getLogger("rekit.notify")

_TIMEOUT = 8
_EVENT_LABELS = {
    "finished": "实验完成",
    "failed": "实验失败",
    "criteria_failed": "判据未通过",
    "budget_exceeded": "预算超限",
    "gpu_offline": "GPU 掉线",
    "share_comment": "收到访客批注",
}


def _quiet_now(quiet):
    """quiet 形如 '22:00-08:00'，跨午夜也支持。"""
    if not quiet or "-" not in quiet:
        return False
    try:
        a, b = [s.strip() for s in quiet.split("-", 1)]
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
        now = datetime.datetime.now()
        cur = now.hour * 60 + now.minute
        start, end = ah * 60 + am, bh * 60 + bm
        return (cur >= start or cur < end) if start > end else (start <= cur < end)
    except Exception:
        return False


def _payload(event, title, message, exp_url):
    """兼容企业微信/钉钉 markdown 与通用 JSON 两种格式。"""
    label = _EVENT_LABELS.get(event, event)
    text = f"**[{label}]** {title}\n\n{message}\n\n[查看实验]({exp_url})" if exp_url else f"**[{label}]** {title}\n\n{message}"
    return {
        "msgtype": "markdown",
        "markdown": {"content": text},
        "text": {"content": text.replace("**", "")},
        "event": event, "title": title, "message": message, "url": exp_url,
    }


def _post(url, payload):
    try:
        r = requests.post(url, json=payload, timeout=_TIMEOUT)
        if r.status_code >= 400:
            log.warning("webhook HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("webhook 发送失败: %s", crypto.redact(str(e)))


def send(event, exp_id, message="", title=None, url_path=""):
    """发送通知。event 须在配置的 events 白名单内。异步执行。"""
    def run():
        try:
            exp = db.get_experiment(exp_id)
            if not exp:
                return
            uid = exp["user_id"]
            cfg = config.load_config(uid).get("notify") or {}
            if not cfg.get("enabled") or not cfg.get("webhook_url"):
                return
            if event not in (cfg.get("events") or []):
                return
            if _quiet_now(cfg.get("quiet_hours", "")):
                return
            server = config.load_config(uid).get("server") or {}
            base = f"{server.get('scheme','https')}://{server.get('host','127.0.0.1')}:{server.get('port',7788)}"
            exp_url = base + url_path if url_path else ""
            payload = _payload(event, title or exp.get("title", "实验"), message, exp_url)
            _post(cfg["webhook_url"], payload)
        except Exception as e:
            log.warning("通知处理异常: %s", crypto.redact(str(e)))

    threading.Thread(target=run, daemon=True).start()


def send_to_user(uid, event, message="", title="", url_path=""):
    """已知 uid 时直接发送（用于非实验级事件）。"""
    def run():
        try:
            cfg = config.load_config(uid).get("notify") or {}
            if not cfg.get("enabled") or not cfg.get("webhook_url"):
                return
            if event not in (cfg.get("events") or []):
                return
            if _quiet_now(cfg.get("quiet_hours", "")):
                return
            _post(cfg["webhook_url"], _payload(event, title, message, url_path))
        except Exception as e:
            log.warning("通知处理异常: %s", crypto.redact(str(e)))

    threading.Thread(target=run, daemon=True).start()
