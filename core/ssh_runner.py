"""SSH 远程执行引擎：上传代码 → 建环境 → 后台启动 → 轮询状态 → 增量读日志。

设计要点：
- 长任务（pip install / 训练）一律后台跑 + 轮询退出码文件，避免 SSH 通道超时。
- 每个 poll 新建一条 SSH 连接（3 秒一次，开销可接受），避免长时间空闲被中断。
- 日志用 sftp 按 offset 增量读，不会把整个日志反复拉回来。
"""
import hashlib
import json
import os
import posixpath
import random
import re
import shlex
import socket
import threading
import time

import paramiko

import config
from core import db, workspace

METRIC_RE = re.compile(r"@@METRIC\s+(.+)$")
KV_RE = re.compile(r"(\w+)\s*=\s*\"?([^\s\"]+)\"?")
FALLBACK_METRIC_RE = re.compile(
    r"\b(loss|acc|accuracy|f1|auc|precision|recall|bleu|rouge-?[lL1]|exact_match|em|map)\b"
    r"\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", re.I)


class SSHError(Exception):
    pass


def connect(host, timeout=20):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(
        hostname=host["host"],
        port=int(host.get("port") or 22),
        username=host["username"],
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    if host.get("auth_type") == "key":
        kw["key_filename"] = os.path.expanduser(host.get("key_path") or "~/.ssh/id_rsa")
        if host.get("password"):
            kw["passphrase"] = host["password"]
    else:
        kw["password"] = host.get("password") or ""
    try:
        client.connect(**kw)
    except paramiko.AuthenticationException:
        raise SSHError("SSH 认证失败，检查用户名/密码/私钥")
    except socket.timeout:
        raise SSHError(f"连接 {host['host']}:{kw['port']} 超时，检查网络或端口")
    except Exception as e:
        raise SSHError(f"SSH 连接失败：{e}")
    return client


# ---------- 连接复用（带 keepalive 与失效检测） ----------
_client_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 240  # 4 分钟复用窗口


def _host_key(host):
    return f"{host['host']}:{host.get('port', 22)}:{host['username']}:{host.get('auth_type', 'password')}"


def _alive(client):
    try:
        transport = client.get_transport()
        return bool(transport and transport.is_active())
    except Exception:
        return False


def get_cached_client(host, timeout=20):
    """复用同一台主机上的 SSH 连接，减少每轮握手。失效时重建。"""
    key = _host_key(host)
    with _cache_lock:
        entry = _client_cache.get(key)
        now = time.time()
        if entry and now - entry["ts"] < _CACHE_TTL and _alive(entry["client"]):
            entry["ts"] = now
            return entry["client"]
        if entry and entry["client"]:
            try:
                entry["client"].close()
            except Exception:
                pass
    client = connect_with_retry(host, timeout=timeout)
    try:
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)  # 每 30s 心跳，防止空闲断开
    except Exception:
        pass
    with _cache_lock:
        _client_cache[key] = {"client": client, "ts": time.time()}
    return client


def drop_cached_client(host):
    key = _host_key(host)
    with _cache_lock:
        entry = _client_cache.pop(key, None)
    if entry and entry["client"]:
        try:
            entry["client"].close()
        except Exception:
            pass


def clear_client_cache():
    with _cache_lock:
        entries = list(_client_cache.values())
        _client_cache.clear()
    for entry in entries:
        try:
            entry["client"].close()
        except Exception:
            pass


def connect_with_retry(host, timeout=20, retries=3, base_delay=1.0):
    """连接失败时指数退避重试；认证失败不重试。"""
    last = None
    for attempt in range(retries + 1):
        try:
            return connect(host, timeout=timeout)
        except SSHError as e:
            last = e
            if "认证失败" in str(e):
                raise  # 认证问题重试无意义
            if attempt < retries:
                time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.3))
    raise last


# ---------- SFTP 断点续传 ----------
def upload_file(sftp, local_path, remote_path, emit=None, chunk=128 * 1024):
    """大文件断点续传：先比对远端 .part 大小，从断点继续，最后原子重命名。"""
    size = os.path.getsize(local_path)
    part = remote_path + ".part"
    offset = 0
    try:
        offset = int(sftp.stat(part).st_size)
    except IOError:
        offset = 0
    # 远端已完成且大小一致则跳过
    try:
        if int(sftp.stat(remote_path).st_size) == size:
            if emit:
                emit(f"跳过未变更文件：{os.path.basename(remote_path)}")
            return
    except IOError:
        pass
    if offset > size:  # 远端脏数据，从头来
        offset = 0
    mode = "ab" if offset else "wb"
    with open(local_path, "rb") as lf, sftp.open(part, mode) as rf:
        if offset:
            lf.seek(offset)
            if emit:
                emit(f"断点续传 {os.path.basename(remote_path)}：从 {offset} B 继续")
        while True:
            data = lf.read(chunk)
            if not data:
                break
            rf.write(data)
    sftp.rename(part, remote_path)
    if emit:
        emit(f"上传 {os.path.basename(remote_path)} ({size} B)")


def run(client, cmd, timeout=60, login_shell=True):
    """执行命令并返回 (exit_code, stdout, stderr)。"""
    full = f"bash -lc {shlex.quote(cmd)}" if login_shell else cmd
    try:
        stdin, stdout, stderr = client.exec_command(full, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except socket.timeout:
        raise SSHError("远程命令超时")


def test_connection(host):
    """连通性自检，返回环境信息。"""
    client = connect(host)
    try:
        info = {}
        code, out, _ = run(client, "uname -srm; echo '---'; nproc 2>/dev/null || echo '?'; "
                                   "echo '---'; free -g 2>/dev/null | head -2; echo '---'; "
                                   f"{host.get('python_bin') or 'python3'} --version 2>&1; echo '---'; "
                                   "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'no-gpu'")
        parts = [p.strip() for p in out.split("---")]
        info["os"] = parts[0] if parts else ""
        info["cpu"] = parts[1] if len(parts) > 1 else ""
        info["mem"] = parts[2] if len(parts) > 2 else ""
        info["python"] = parts[3] if len(parts) > 3 else ""
        info["gpu"] = parts[4] if len(parts) > 4 else "no-gpu"
        info["has_gpu"] = "no-gpu" not in info["gpu"] and info["gpu"].strip() != ""
        return info
    finally:
        client.close()


# ---------- 部署 ----------

SETUP_SH = r"""#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
rm -f .setup.exit
PYBASE=__PYBASE__
FAIL=0

echo "[setup] 基础解释器: $($PYBASE --version 2>&1)"

if [ ! -x .venv/bin/python ]; then
  echo "[setup] 创建虚拟环境 .venv"
  $PYBASE -m venv .venv 2>&1 | tail -20
  if [ ! -x .venv/bin/python ]; then
    echo "[setup] venv 创建失败（可能缺 python3-venv），退化为系统 Python"
  fi
fi

PY=".venv/bin/python"
PIP=".venv/bin/pip"
[ -x "$PY" ] || PY="$PYBASE"
if [ -x "$PIP" ]; then
  $PIP install --upgrade pip -q 2>&1 | tail -3
fi

if [ -s requirements.txt ] && grep -qEv '^\s*(#|$)' requirements.txt; then
  echo "[setup] 安装依赖（这一步可能比较久）"
  INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  if [ -x "$PIP" ]; then
    $PIP install -i "$INDEX" --timeout 120 -r requirements.txt 2>&1 | tail -40
    rc=${PIPESTATUS[0]}
    if [ "$rc" != "0" ]; then
      echo "[setup] 镜像源安装失败(rc=$rc)，改用默认源重试"
      $PIP install --timeout 120 -r requirements.txt 2>&1 | tail -40
      rc=${PIPESTATUS[0]}
    fi
  else
    $PY -m pip install -i "$INDEX" --timeout 120 -r requirements.txt 2>&1 | tail -40
    rc=${PIPESTATUS[0]}
  fi
  if [ "$rc" != "0" ]; then
    echo "[setup] ✕ 依赖安装失败 rc=$rc"
    FAIL=1
  else
    echo "[setup] ✓ 依赖安装完成"
  fi
else
  echo "[setup] requirements.txt 为空，跳过依赖安装"
fi

echo "[setup] 环境信息"
$PY -c "import sys; print('[setup] python', sys.version.split()[0])" 2>&1
$PY -c "import torch; print('[setup] torch', torch.__version__, 'cuda', torch.cuda.is_available())" 2>&1 | tail -2

echo "$FAIL" > .setup.exit
echo "[setup] done (exit=$FAIL)"
"""

RUN_SH = r"""#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
rm -f .run.exit
PY=".venv/bin/python"
[ -x "$PY" ] || PY=__PYBASE__
echo "$$" > .run.pid
date +%s > .run.start
export SEED=__SEED__
echo "[run] $(date '+%F %T') 启动 $PY __ENTRY__ seed=$SEED"
"$PY" -u __ENTRY__
code=$?
echo "[run] $(date '+%F %T') 进程结束 exit_code=$code"
echo "$code" > .run.exit
rm -f .run.pid
exit $code
"""


def launch_cmd(script, log_file):
    """跨平台后台启动：优先 setsid（Linux），没有就退回 nohup（macOS/BSD）。

    两种方式都用 nohup + stdin 接 /dev/null，确保 SSH 通道关闭后进程继续跑。
    """
    return (
        f"if command -v setsid >/dev/null 2>&1; then "
        f"setsid nohup bash {script} > {log_file} 2>&1 < /dev/null & "
        f"else nohup bash {script} > {log_file} 2>&1 < /dev/null & fi; "
        f"sleep 1; echo started"
    )


def deploy_and_launch(run_id, host_id, experiment_id, code, seed=None):
    """整个部署流程。同步执行，由调用方放到线程里跑。"""
    host = db.get_host(host_id, include_secrets=True)
    exp = db.get_experiment(experiment_id)
    cfg = config.load_config()
    interval = cfg["monitor"]["interval_sec"]

    def emit(msg, level="info"):
        bus_log(experiment_id, msg, run_id)
        db.add_event(experiment_id, run_id, msg, kind="stage", level=level)

    base = (host.get("workdir") or "~/research-runs").rstrip("/")
    remote_dir = f"{base}/exp{experiment_id}_r{run_id}"
    local_dir = workspace.exp_dir(experiment_id)
    db.update_run(run_id, status="uploading", remote_dir=remote_dir, local_dir=local_dir)
    bus_status(experiment_id, "uploading", run_id)

    client = None
    try:
        emit(f"连接 {host['username']}@{host['host']}:{host.get('port') or 22}")
        client = connect_with_retry(host)
        emit("SSH 连接成功")

        rc, _, err = run(client, f"mkdir -p {shlex.quote(remote_dir)}")
        if rc != 0:
            raise SSHError(f"创建远程目录失败：{err}")

        # 上传
        sftp = client.open_sftp()
        files = [f["path"] for f in code.get("files", [])]
        for rel in files:
            local_path = os.path.join(local_dir, rel)
            if not os.path.exists(local_path):
                continue
            remote_path = posixpath.join(remote_dir, rel)
            parent = posixpath.dirname(remote_path)
            if parent and parent != remote_dir:
                run(client, f"mkdir -p {shlex.quote(parent)}")
            upload_file(sftp, local_path, remote_path, emit=emit)

        pybase = host.get("python_bin") or "python3"
        entry = code.get("entry") or "main.py"
        setup_sh = SETUP_SH.replace("__PYBASE__", pybase)
        run_sh = (RUN_SH.replace("__PYBASE__", pybase)
                  .replace("__ENTRY__", entry)
                  .replace("__SEED__", str(seed if seed is not None else 42)))
        with sftp.open(posixpath.join(remote_dir, "setup.sh"), "w") as f:
            f.write(setup_sh)
        with sftp.open(posixpath.join(remote_dir, "run.sh"), "w") as f:
            f.write(run_sh)
        sftp.close()

        # 建环境（后台 + 轮询）
        db.update_run(run_id, status="setup")
        bus_status(experiment_id, "setup", run_id)
        emit("开始准备运行环境（创建 venv / 安装依赖）")
        run(client, f"cd {shlex.quote(remote_dir)} && chmod +x setup.sh run.sh && "
                    f"rm -f .setup.exit .setup.pid setup.log && "
                    + launch_cmd("setup.sh", "setup.log"))

        _poll_until(client, remote_dir, ".setup.exit", experiment_id, run_id,
                    "setup.log", interval, timeout=3600)
        rc, out, _ = run(client, f"cd {shlex.quote(remote_dir)} && cat .setup.exit 2>/dev/null || echo 1")
        setup_rc = out.strip()
        if setup_rc != "0":
            raise SSHError(f"环境准备失败（退出码 {setup_rc}），请看上方日志。可以先在代码里减少依赖。")
        emit("环境就绪")

        # 环境固化：精确依赖 + 硬件快照，随 run 存档
        try:
            run_row = db.get_run(run_id) or {}
            env_snapshot = {}
            try:
                env_snapshot = json.loads(run_row.get("env_json") or "{}")
            except Exception:
                env_snapshot = {}
            _, pip_out, _ = run(
                client,
                f"cd {shlex.quote(remote_dir)} && .venv/bin/python -m pip freeze 2>/dev/null | head -200",
                timeout=60)
            _, gpu_out, _ = run(client, "nvidia-smi 2>/dev/null | head -40 || true", timeout=30)
            _, py_out, _ = run(
                client,
                f"cd {shlex.quote(remote_dir)} && .venv/bin/python -c "
                "\"import sys; print(sys.version.split()[0])\" 2>/dev/null || true",
                timeout=30)
            env_snapshot.update({
                "seed": seed if seed is not None else env_snapshot.get("seed", 42),
                "python": py_out.strip(),
                "pip_freeze": pip_out.strip(),
                "nvidia_smi": gpu_out.strip(),
                "remote_dir": remote_dir,
                "host": f"{host.get('username')}@{host.get('host')}:{host.get('port') or 22}",
                "captured_at": time.time(),
            })
            db.update_run(run_id, env_json=json.dumps(env_snapshot, ensure_ascii=False))
            emit("已保存 seed / 依赖版本 / 硬件环境快照")
        except Exception as e:
            emit(f"环境快照采集失败（不影响运行）：{e}", "warn")

        # 启动实验
        db.update_run(run_id, status="running")
        bus_status(experiment_id, "running", run_id)
        emit("启动实验进程")
        run(client, f"cd {shlex.quote(remote_dir)} && rm -f run.log .run.exit .run.pid && "
                    + launch_cmd("run.sh", "run.log"))
        time.sleep(1.2)
        rc, out, _ = run(client, f"cd {shlex.quote(remote_dir)} && cat .run.pid 2>/dev/null || echo")
        pid = out.strip()
        db.update_run(run_id, pid=pid)
        emit(f"实验已启动，PID={pid or '未知'}，日志：{remote_dir}/run.log")
        db.update_experiment(experiment_id, status="running")

        client.close()
        client = None

        # 进入监控循环
        from core import monitor
        monitor.watch(run_id, experiment_id, host_id, remote_dir, interval)

    except Exception as e:
        msg = f"部署失败：{e}"
        bus_log(experiment_id, msg, run_id)
        bus_status(experiment_id, "failed", run_id, {"error": str(e)})
        bus_note(experiment_id, msg, "error", run_id)
        db.add_event(experiment_id, run_id, msg, kind="error", level="error")
        db.update_run(run_id, status="failed", finished_at=db.now())
        db.update_experiment(experiment_id, status="failed")
        if client:
            try:
                client.close()
            except Exception:
                pass
        bus_summary(experiment_id, {"error": str(e)}, run_id)


def _poll_until(client, remote_dir, flag_file, exp_id, run_id, log_file, interval, timeout=3600):
    """等一个 flag 文件出现，期间把日志增量推给前端。"""
    offset = 0
    t0 = time.time()
    sftp = None
    try:
        sftp = client.open_sftp()
        while time.time() - t0 < timeout:
            time.sleep(interval)
            offset = _stream_log(sftp, remote_dir, log_file, offset, exp_id, run_id)
            if _exists(sftp, remote_dir, flag_file):
                offset = _stream_log(sftp, remote_dir, log_file, offset, exp_id, run_id)
                return True
        return False
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _exists(sftp, remote_dir, name):
    try:
        sftp.stat(posixpath.join(remote_dir, name))
        return True
    except IOError:
        return False


def _stream_log(sftp, remote_dir, log_file, offset, exp_id, run_id, sink=None):
    """增量读日志，逐行推给前端，返回新 offset。"""
    path = posixpath.join(remote_dir, log_file)
    try:
        st = sftp.stat(path)
    except IOError:
        return offset
    size = st.st_size
    if size < offset:      # 文件被重写
        offset = 0
    if size == offset:
        return offset
    limit = 256 * 1024
    start = max(offset, size - limit) if size - offset > limit else offset
    try:
        with sftp.open(path, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
    except IOError:
        return offset
    text = data.decode("utf-8", "replace")
    if start != offset:
        # 中间有跳过，只保留完整行
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else ""
    lines = text.split("\n")
    complete = lines[:-1]
    new_offset = size - len(lines[-1].encode("utf-8")) if lines else size
    for ln in complete:
        push_log_line(exp_id, run_id, ln, sink)
    return new_offset


def push_log_line(exp_id, run_id, line, sink=None):
    if line is None:
        return
    line = line.rstrip("\r")
    if not line.strip():
        return
    bus_log(exp_id, line, run_id)
    if sink is not None:
        sink(line)
    _maybe_metric(exp_id, run_id, line)


def _maybe_metric(exp_id, run_id, line):
    m = METRIC_RE.search(line)
    if m:
        body = m.group(1).strip()
        name = value = step = None
        if body.startswith("{"):
            try:
                import json
                d = json.loads(body)
                name = d.get("name")
                value = d.get("value")
                step = d.get("step")
            except Exception:
                return
        else:
            kv = dict(KV_RE.findall(body))
            name = kv.get("name")
            value = kv.get("value")
            step = kv.get("step")
        if name and value is not None:
            try:
                fv = float(value)
            except (TypeError, ValueError):
                return
            try:
                fs = float(step) if step not in (None, "") else None
            except (TypeError, ValueError):
                fs = None
            db.add_metric(run_id, str(name), fv, fs)
            bus_metric(exp_id, str(name), fv, fs, run_id)
        return
    # 兜底：识别常见的 loss/acc 写法（只在含 epoch/step 或 stage 关键词时）
    if re.search(r"(epoch|step|iter|loss|acc)", line, re.I):
        for nm, val in FALLBACK_METRIC_RE.findall(line):
            try:
                db.add_metric(run_id, nm.lower(), float(val), None)
                bus_metric(exp_id, nm.lower(), float(val), None, run_id)
            except Exception:
                pass


def read_tail(run_id, n=200):
    """从 runs.log_tail 取历史日志。"""
    r = db.get_run(run_id)
    if not r or not r.get("log_tail"):
        return []
    return r["log_tail"].split("\n")[-n:]


def append_tail(run_id, line):
    """单行追加（保留兼容）。"""
    flush_tail(run_id, [line])


def flush_tail(run_id, lines):
    """把一批日志行并入 runs.log_tail，只保留最近 300 行。

    每轮 poll 调用一次，避免每行日志都写一次库。
    """
    if not lines:
        return
    r = db.get_run(run_id)
    tail = [x for x in ((r.get("log_tail") or "").split("\n")) if x] if r else []
    tail.extend(lines)
    db.update_run(run_id, log_tail="\n".join(tail[-300:]))


def parse_gpu(out):
    gpus = []
    for ln in (out or "").strip().split("\n"):
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) >= 5 and parts[0].isdigit():
            try:
                gpus.append({
                    "index": parts[0],
                    "name": parts[1],
                    "util": float(parts[2]),
                    "mem_used": float(parts[3]),
                    "mem_total": float(parts[4]),
                })
            except ValueError:
                continue
    return gpus


# 延迟导入，避免循环依赖
def bus_log(exp_id, msg, run_id=None):
    from core import bus
    bus.log(exp_id, msg, run_id)


def bus_status(exp_id, st, run_id=None, extra=None):
    from core import bus
    bus.status(exp_id, st, run_id, extra)


def bus_metric(exp_id, name, value, step, run_id):
    from core import bus
    bus.metric(exp_id, name, value, step, run_id)


def bus_note(exp_id, msg, level, run_id):
    from core import bus
    bus.note(exp_id, msg, level, run_id)


def bus_summary(exp_id, data, run_id):
    from core import bus
    bus.summary(exp_id, data, run_id)
