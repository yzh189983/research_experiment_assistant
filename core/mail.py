"""邮件发送：基于标准库 smtplib，用于账户密码重置链接。

配置来自 config.py 的 mail 段（系统级）。未启用或未配置时静默跳过，
绝不在日志里打印 SMTP 凭据或邮件正文里的重置链接。
"""
import smtplib
import ssl
from email.message import EmailMessage

import config
from core import crypto


class MailError(RuntimeError):
    """邮件发送失败。"""


def is_enabled():
    cfg = config.load_global_config().get("mail") or {}
    return bool(cfg.get("enabled")) and bool(cfg.get("host"))


def _mail_config():
    cfg = config.load_global_config().get("mail") or {}
    return {
        "host": str(cfg.get("host") or "").strip(),
        "port": int(cfg.get("port") or 587),
        "username": str(cfg.get("username") or "").strip(),
        "password": str(cfg.get("password") or "").strip(),
        "from_addr": str(cfg.get("from_addr") or "").strip() or str(cfg.get("username") or "").strip(),
        "use_ssl": bool(cfg.get("use_ssl")),
        "timeout": int(cfg.get("timeout") or 10),
    }


def send_mail(to_addr, subject, body, html=None):
    """发送一封邮件。to_addr/subject/body 由调用方构造。"""
    cfg = _mail_config()
    if not cfg["host"] or not to_addr:
        raise MailError("邮件未配置或收件人为空")
    msg = EmailMessage()
    msg["From"] = cfg["from_addr"] or "noreply@rekit.local"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        if cfg["use_ssl"]:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"],
                                  context=ssl.create_default_context()) as s:
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
                if cfg["username"]:
                    s.login(cfg["username"], cfg["password"])
                s.send_message(msg)
    except Exception as exc:
        # 只记录脱敏后的异常文本，绝不打印 msg / 密码 / 链接
        raise MailError("邮件发送失败：%s" % crypto.redact(str(exc)))


def send_password_reset(to_addr, reset_url, ttl_minutes=30):
    """发送密码重置邮件。"""
    subject = "科研实验助手 · 重置密码"
    body = (
        "你（或有人）在科研实验助手请求重置密码。\n\n"
        "请在 %d 分钟内打开下面的链接设置新密码：\n%s\n\n"
        "如果这不是你本人操作，请忽略此邮件，链接会自动失效。\n"
        "该链接仅可使用一次。\n" % (ttl_minutes, reset_url)
    )
    html = (
        '<div style="font-family:-apple-system,Segoe UI,PingFang SC,sans-serif;line-height:1.7">'
        '<h2 style="color:#0F5F46">重置密码</h2>'
        '<p>你（或有人）在科研实验助手请求重置密码。</p>'
        '<p>请在 %d 分钟内点击下面的按钮设置新密码（仅可使用一次）：</p>'
        '<p><a href="%s" style="display:inline-block;padding:10px 18px;background:#0F5F46;'
        'color:#fff;text-decoration:none;border-radius:3px">重置密码</a></p>'
        '<p style="color:#888;font-size:12px">如果这不是你本人操作，请忽略此邮件，链接会自动失效。<br>'
        '若按钮无法点击，可复制此链接到浏览器：<br>%s</p></div>'
    ) % (ttl_minutes, reset_url, reset_url)
    send_mail(to_addr, subject, body, html=html)
