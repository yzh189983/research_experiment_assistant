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

from core import workspace

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d59288fbee4904"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SNAPSHOT_FILE = ".rekit_snapshot.json"


def available():
    return shutil.which("git") is not None


def _git_raw(cwd, *args, check=True, timeout=30):
    if not available():
        raise RuntimeError("本机未找到 git，无法创建代码快照")
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Rekit",
        "GIT_AUTHOR_EMAIL": "rekit@local",
        "GIT_COMMITTER_NAME": "Rekit",
        "GIT_COMMITTER_EMAIL": "rekit@local",
    })
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git 命令失败").strip())
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
