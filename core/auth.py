"""鉴权层：session（浏览器）+ Bearer token（CLI/脚本）+ WebSocket 握手校验。

依赖 werkzeug 的密码哈希（Flask 自带，无需额外装包）。
"""
import functools
import hashlib
import hmac
import base64
import hashlib
import hmac
import os
import secrets
import time

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
from core import crypto, db

SESSION_KEY = "uid"
TOKEN_FILE_NAME = "cli_token"


def token_path():
    import os
    return os.path.join(config.DATA_DIR, TOKEN_FILE_NAME)


# ---------------- 密码 ----------------
def hash_password(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256:260000")


def verify_password(user, pw):
    try:
        return check_password_hash(user["password_hash"], pw)
    except Exception:
        return False


# ---------------- API token ----------------
def _token_digest(token, salt):
    return hashlib.sha256(f"{salt}:{token}".encode()).hexdigest()


def issue_token(uid, save_local=False):
    """生成新 token，只把摘要存库，明文返回给调用方。"""
    token = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    db.update_user(uid, api_token_hash=_token_digest(token, salt), token_salt=salt)
    if save_local:
        import os
        config.ensure_dirs()
        p = token_path()
        with open(p, "w", encoding="utf-8") as f:
            f.write(crypto.encrypt(token.strip()))
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    return token


def revoke_token(uid):
    db.update_user(uid, api_token_hash="", token_salt="")
    try:
        import os
        os.remove(token_path())
    except Exception:
        pass


def read_local_token():
    try:
        with open(token_path(), "r", encoding="utf-8") as f:
            return crypto.decrypt(f.read().strip())
    except Exception:
        return ""


def migrate_local_token():
    """把历史上明文保存的 data/cli_token 原地加密。"""
    try:
        with open(token_path(), "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return
    if not raw or raw.startswith(crypto.PREFIX):
        return
    with open(token_path(), "w", encoding="utf-8") as f:
        f.write(crypto.encrypt(raw))
    try:
        import os
        os.chmod(token_path(), 0o600)
    except Exception:
        pass


def user_by_token(token):
    if not token:
        return None
    rows = db.q("SELECT * FROM users WHERE api_token_hash!=''")
    for r in rows:
        u = dict(r)
        if hmac.compare_digest(_token_digest(token, u.get("token_salt") or ""),
                               u["api_token_hash"]):
            return u
    return None


# ---------------- 登录态解析 ----------------
def _bearer_token():
    h = request.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return ""


def current_user():
    """返回当前登录用户 dict，未登录返回 None。优先 session，其次 Bearer/query token。"""
    if getattr(g, "_auth_user", None) is not None:
        return g._auth_user
    user = None
    uid = session.get(SESSION_KEY)
    if uid:
        user = db.get_user(uid)
    if not user:
        token = _bearer_token() or request.args.get("token", "")
        if token:
            user = user_by_token(token)
    if user:
        g._auth_user = user
        # 同步到 config 上下文，db 层据此自动过滤
        config.set_current_uid(user["id"])
    return user


def login_user(uid, remember=False):
    session.clear()
    session[SESSION_KEY] = int(uid)
    session.permanent = bool(remember)
    db.update_user(uid, last_login=db.now())
    g._auth_user = db.get_user(uid)
    config.set_current_uid(int(uid))


def logout_user():
    session.clear()
    g._auth_user = None
    config.set_current_uid(None)


# ---------------- 装饰器 ----------------
def login_required(fn):
    """页面用：未登录跳转 /login，记录 next。"""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login_page", next=request.full_path.rstrip("?&")))
        return fn(*args, **kwargs)

    return wrapped


def api_required(fn):
    """API 用：未登录返回 401 JSON。"""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "未登录或登录已失效，请先登录"}), 401
        return fn(*args, **kwargs)

    return wrapped


# ---------------- 初始化 ----------------
def ensure_admin():
    """首次启动创建 admin，密码取环境变量 REKIT_ADMIN_PASSWORD，否则随机并落盘提示。"""
    if db.count_users() > 0:
        return None
    import os
    pw = os.environ.get("REKIT_ADMIN_PASSWORD") or secrets.token_urlsafe(10)
    uid = db.add_user("admin", hash_password(pw), role="admin")
    if not os.environ.get("REKIT_ADMIN_PASSWORD"):
        p = os.path.join(config.DATA_DIR, "admin_initial_password.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"username: admin\npassword: {crypto.encrypt(pw)}\n")
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        print("\n  [auth] 已创建管理员 admin，初始密码已加密；用 ./rekit admin-password 查看\n")
    return pw


def migrate_admin_password_file():
    """把历史上明文保存的初始密码文件原地加密。"""
    path = os.path.join(config.DATA_DIR, "admin_initial_password.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return
    changed = False
    for i, line in enumerate(lines):
        if line.strip().startswith("password:"):
            pw = line.split(":", 1)[1].strip()
            if pw and not pw.startswith(crypto.PREFIX):
                lines[i] = f"password: {crypto.encrypt(pw)}\n"
                changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)


def get_initial_admin_password():
    """读取并解密 admin 初始密码文件（若已改密，此值可能失效）。"""
    try:
        with open(os.path.join(config.DATA_DIR, "admin_initial_password.txt"),
                  "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("password:"):
                    return crypto.decrypt(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return ""


# ---------------- 只读观察链接 ----------------
def _observe_key():
    return hashlib.sha256(config.get_or_create_secret_key().encode("utf-8")).digest()


def make_observe_token(uid, exp_id, ttl_sec=7 * 24 * 3600):
    expires = int(time.time()) + int(ttl_sec)
    payload = f"{uid}:{exp_id}:{expires}"
    sig = hmac.new(_observe_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def verify_observe_token(token):
    """返回 (uid, exp_id)，签名无效或过期返回 None。"""
    try:
        raw = base64.urlsafe_b64decode(str(token) + "=" * (-len(token) % 4)).decode("utf-8")
        uid_s, exp_s, expires_s, sig = raw.split(":")
        payload = f"{uid_s}:{exp_s}:{expires_s}"
        expected = hmac.new(_observe_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected) or int(expires_s) < time.time():
            return None
        return int(uid_s), int(exp_s)
    except Exception:
        return None
