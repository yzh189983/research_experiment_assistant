"""GitHub 轻量客户端：校验 Token / 仓库写权限，仅用标准库之外已依赖的 requests。"""
import re

import requests

from core import crypto

_API = "https://api.github.com"
_TIMEOUT = 15
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def check_repo_access(token, repo_full_name):
    """校验 Token 与仓库写权限，返回 (login, repo_full_name)。

    抛 RuntimeError 携带用户可读原因；错误信息统一脱敏。
    """
    repo = str(repo_full_name or "").strip().strip("/")
    if not _REPO_RE.fullmatch(repo) or any(s in (".", "..") for s in repo.split("/")):
        raise RuntimeError("仓库名格式应为 owner/repo")
    token = str(token or "").strip()
    if not token:
        raise RuntimeError("请先填写 GitHub Token")
    crypto.remember_secret(token)

    try:
        r = requests.get(f"{_API}/user", headers=_headers(token), timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError("连接 GitHub 失败：" + crypto.redact(str(e)))
    if r.status_code != 200:
        raise RuntimeError(_auth_error(r.status_code))

    login = r.json().get("login", "")

    try:
        r = requests.get(f"{_API}/repos/{repo}", headers=_headers(token), timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError("查询仓库失败：" + crypto.redact(str(e)))
    if r.status_code == 404:
        raise RuntimeError("仓库不存在或 Token 无权访问该仓库")
    if r.status_code != 200:
        raise RuntimeError(_auth_error(r.status_code))

    perms = (r.json() or {}).get("permissions") or {}
    if not perms.get("push") and not perms.get("admin"):
        raise RuntimeError("Token 对该仓库没有写权限（需要 Contents: read and write）")
    return login, repo


def _auth_error(status):
    if status in (401, 403):
        return "Token 无效或已过期（HTTP %d）" % status
    return "GitHub 接口返回 HTTP %d" % status
