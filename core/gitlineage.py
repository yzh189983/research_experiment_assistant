"""实验工作区的本地 git 快照与谱系。

每个实验目录 data/workspace/exp_<id> 是一个独立 git 仓库；
每次 run 前锁定一个 commit，并把这个 SHA 写到 runs.git_sha。
"""
import json
import os
import re
import shutil
import subprocess
import time

from core import crypto, workspace

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d59288fbee4904"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SNAPSHOT_FILE = ".rekit_snapshot.json"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def available():
    return shutil.which("git") is not None


def _git_raw(cwd, *args, check=True, timeout=30, no_credential=False):
    if not available():
        raise RuntimeError("本机未找到 git，无法创建代码快照")
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Rekit",
        "GIT_AUTHOR_EMAIL": "rekit@local",
        "GIT_COMMITTER_NAME": "Rekit",
        "GIT_COMMITTER_EMAIL": "rekit@local",
    })
    if no_credential:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
    cmd = ["git", *args]
    if no_credential:
        cmd = ["git", "-c", "credential.helper=", *args]
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(crypto.redact((proc.stderr or proc.stdout or "git 命令失败").strip()))
    return proc.stdout


def _git(cwd, *args, **kwargs):
    return _git_raw(cwd, *args, **kwargs).strip()


def _sha(value, allow_empty=False):
    if not value and allow_empty:
        return ""
    value = str(value or "").lower()
    if not _SHA_RE.fullmatch(value):
        raise ValueError("无效的 commit SHA")
    return value


def ensure_repo(eid):
    d = workspace.exp_dir(eid)
    if not os.path.isdir(os.path.join(d, ".git")):
        _git(d, "init", "-q")
        _git(d, "config", "user.name", "Rekit")
        _git(d, "config", "user.email", "rekit@local")
    return d


def commit_snapshot(eid, code, run_id=None, message=""):
    """写入快照元数据并提交当前工作区，返回完整 commit SHA。"""
    d = ensure_repo(eid)
    code = code or {}
    meta = {k: v for k, v in code.items() if k != "files"}
    meta["files"] = [f.get("path") for f in code.get("files", []) if f.get("path")]
    meta["run_id"] = run_id
    meta["committed_at"] = time.time()
    meta["message"] = message
    workspace.save_file(eid, _SNAPSHOT_FILE,
                        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    _git(d, "add", "-A")
    subject = f"rekit run #{run_id} snapshot" if run_id else f"rekit {message or 'snapshot'}"
    _git(d, "commit", "-q", "--allow-empty", "-m", subject)
    return _git(d, "rev-parse", "HEAD")


def history(eid, limit=50):
    d = ensure_repo(eid)
    fmt = "%H%x1f%h%x1f%s%x1f%ct"
    out = _git(d, "log", "-n", str(max(1, min(int(limit or 50), 200))),
               f"--pretty=format:{fmt}", "--", check=False)
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({"sha": parts[0], "short": parts[1],
                            "subject": parts[2], "ts": int(parts[3] or 0)})
    return commits


def diff(eid, base_sha, target_sha):
    d = ensure_repo(eid)
    target = _sha(target_sha)
    base = _sha(base_sha, allow_empty=True) or EMPTY_TREE
    return _git(d, "diff", "--find-renames", base, target, "--")


def files_at(eid, sha):
    d = ensure_repo(eid)
    sha = _sha(sha)
    out = _git(d, "ls-tree", "-r", "--name-only", sha, "--")
    files = []
    for path in out.splitlines():
        if not path or path.startswith("."):
            continue
        content = _git_raw(d, "show", f"{sha}:{path}")
        files.append({"path": path, "content": content})
    return files


def code_at(eid, sha):
    d = ensure_repo(eid)
    sha = _sha(sha)
    meta = {}
    try:
        meta = json.loads(_git_raw(d, "show", f"{sha}:{_SNAPSHOT_FILE}"))
    except Exception:
        pass
    code = {k: v for k, v in meta.items()
            if k not in ("files", "run_id", "committed_at", "message")}
    code["files"] = files_at(eid, sha)
    if not code.get("entry") and code["files"]:
        code["entry"] = code["files"][0]["path"]
    if not code.get("run_cmd"):
        code["run_cmd"] = f"python {code['entry']}" if code.get("entry") else "python main.py"
    return code


def restore(eid, sha):
    """把工作区恢复成指定 commit 的内容，并清掉之后新增的未跟踪文件。"""
    d = ensure_repo(eid)
    sha = _sha(sha)
    for path in _git(d, "ls-files", "--others", "--exclude-standard").splitlines():
        rel = os.path.normpath(path)
        if not rel or rel.startswith("..") or os.path.isabs(rel):
            continue
        target = os.path.join(d, rel)
        if os.path.isfile(target):
            os.remove(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
    _git(d, "read-tree", "--reset", "-u", sha)


def clone_for_fork(src_eid, dst_eid, sha, branch):
    """把源仓库克隆到新实验目录，并停在指定 commit 的派生分支。"""
    src = ensure_repo(src_eid)
    dst = workspace.exp_dir(dst_eid)
    sha = _sha(sha)
    branch = "fork-" + re.sub(r"[^0-9A-Za-z_-]", "-", str(branch))[:60]
    try:
        _git(os.path.dirname(dst), "clone", "-q", "--no-hardlinks", src, dst)
    except RuntimeError:
        for name in os.listdir(src):
            if name == ".git":
                continue
            s, t = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s):
                shutil.copytree(s, t, dirs_exist_ok=True)
            else:
                shutil.copy2(s, t)
        ensure_repo(dst_eid)
    _git(dst, "checkout", "-q", "-b", branch, sha)
    return dst


# ---------- GitHub remote / push ----------
_SENSITIVE_PATTERNS = (
    ("API Key", re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_\-]{12,}|api[_-]?key\s*[:=]\s*\S{6,})")),
    ("密码", re.compile(r"(?i)\b(?:password|passwd|passphrase)\s*[:=]\s*\S{4,}")),
    ("Bearer Token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}")),
    ("内网地址", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")),
    ("绝对路径", re.compile(r"(?:^|[=\s(\"'])/(?:Users|home|root|mnt|data)/[^\s\"'()]+")),
)


def validate_repo_full_name(value):
    """校验 owner/repo 格式，防止注入到 URL 或命令。"""
    value = str(value or "").strip().strip("/")
    if not _REPO_RE.fullmatch(value):
        raise ValueError("仓库名格式应为 owner/repo（字母、数字、_ . -）")
    if any(seg in (".", "..") for seg in value.split("/")):
        raise ValueError("仓库名格式应为 owner/repo")
    return value


def validate_branch(value):
    value = str(value or "main").strip() or "main"
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,100}", value) or value.startswith(("-", ".")):
        raise ValueError("无效的分支名")
    return value


def scan_sensitive(files, eid=None):
    """扫描代码文件内容，返回可能泄露的敏感信息警告列表。

    files 可为 workspace.list_files() 的元信息；传入 eid 时会自行读取文件内容。
    """
    warnings = []
    seen = set()
    for f in files or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "")
        content = f.get("content")
        if content is None and eid is not None:
            try:
                content = workspace.read_file(eid, path)
            except Exception:
                content = ""
        for label, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(str(content or "")):
                key = (path, label)
                if key not in seen:
                    warnings.append({"path": path, "kind": label})
                    seen.add(key)
    return warnings


def scan_workspace(eid):
    return scan_sensitive(workspace.list_files(eid), eid=eid)


def _remote_arg(repo_full_name, token):
    """构造一次性带 token 的 HTTPS URL；token 不写入 .git/config。"""
    repo = validate_repo_full_name(repo_full_name)
    token = str(token or "").strip()
    if not token:
        raise RuntimeError("GitHub Token 为空")
    crypto.remember_secret(token)
    safe = token.replace("@", "%40").replace("/", "%2F")
    return f"https://x-access-token:{safe}@github.com/{repo}.git"


def set_remote(eid, repo_full_name, token):
    """给实验仓库设置 origin（带 token 的 URL，仅存于本次命令，不落盘）。"""
    d = ensure_repo(eid)
    url = _remote_arg(repo_full_name, token)
    _git(d, "remote", "remove", "origin", check=False)
    _git(d, "remote", "add", "origin", url)
    return d


def remove_remote(eid):
    d = ensure_repo(eid)
    _git(d, "remote", "remove", "origin", check=False)


def push_snapshot(eid, repo_full_name, token, branch="main"):
    """把当前 HEAD 推送到指定 GitHub 仓库的分支，返回 commit SHA。

    使用一次性 URL 传 token，禁用系统凭据助手；出错信息经 redact 脱敏。
    """
    d = ensure_repo(eid)
    url = _remote_arg(repo_full_name, token)
    branch = validate_branch(branch)
    try:
        # 直接以 URL 推送，token 不经 .git/config 落盘
        _git(d, "-c", "http.version=HTTP/1.1", "push", "--quiet", url,
             f"HEAD:refs/heads/{branch}", timeout=60, no_credential=True)
        sha = _git(d, "rev-parse", "HEAD")
    except subprocess.TimeoutExpired:
        raise RuntimeError("推送超时：连接 GitHub 失败，请稍后重试")
    return sha


def remote_commit_url(repo_full_name, sha):
    repo = validate_repo_full_name(repo_full_name)
    sha = _sha(sha)
    return f"https://github.com/{repo}/commit/{sha}"
