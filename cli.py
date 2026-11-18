#!/usr/bin/env python3
"""rekit —— 科研实验助手命令行。

把 Web 界面能干的事搬到终端：建实验、出方案、写代码、部署跑、盯进度、看报告。
只依赖标准库，服务没起时自动在后台拉起。

    rekit                     概览
    rekit auto -i 想法.md     一句话从想法跑到报告
    rekit run 3 --follow      部署并实时跟随
"""
import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

def _find_root():
    """定位项目根目录（有 app.py 的那层），支持在任意目录调用。"""
    env = os.environ.get("REKIT_ROOT")
    if env and os.path.exists(os.path.join(env, "app.py")):
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(here, "app.py")):
        return here
    cwd = os.getcwd()
    if os.path.exists(os.path.join(cwd, "app.py")):
        return cwd
    return here


ROOT = _find_root()
sys.path.insert(0, ROOT)

import config
from core import auth, crypto

_srv = config.load_global_config()["server"]
HOST = os.environ.get("REKIT_HOST", _srv.get("host", "127.0.0.1"))
PORT = int(os.environ.get("REKIT_PORT", _srv.get("port", 7788)))
SCHEME = os.environ.get("REKIT_SCHEME", _srv.get("scheme", "https"))
BASE = f"{SCHEME}://{HOST}:{PORT}"
SERVER_LOG = os.path.join(ROOT, "data", "server.log")
SERVER_PID = os.path.join(ROOT, "data", "server.pid")
TOKEN_FILE = os.path.join(ROOT, "data", "cli_token")


def _auth_token():
    """令牌优先级：环境变量 > 本地文件。"""
    env = os.environ.get("REKIT_TOKEN")
    if env:
        return env.strip()
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return crypto.decrypt(f.read().strip())
    except Exception:
        return ""


def _save_token(token):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(crypto.encrypt(token.strip()))
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def _clear_token():
    try:
        os.remove(TOKEN_FILE)
    except Exception:
        pass

# ---------------- 终端样式 ----------------

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


class C:
    R = "\033[0m"
    B = "\033[1m"
    D = "\033[2m"
    G = "\033[32m"
    RE = "\033[31m"
    Y = "\033[33m"
    BL = "\033[34m"
    CY = "\033[36m"
    GY = "\033[90m"


def c(text, *styles):
    if _NO_COLOR:
        return str(text)
    return "".join(styles) + str(text) + C.R


def say(msg=""):
    print(msg, flush=True)


def ok(msg):
    say(f"  {c('✓', C.G)} {msg}")


def err(msg):
    say(f"  {c('✕', C.RE)} {msg}")


def warn(msg):
    say(f"  {c('!', C.Y)} {msg}")


def hint(msg):
    say(f"  {c(msg, C.GY)}")


def title(msg):
    say(f"\n{c(msg, C.B)}")


def rule():
    w = min(shutil.get_terminal_size((80, 20)).columns, 76)
    say(c("─" * w, C.GY))


# ---------------- HTTP ----------------


def _opener():
    """本地自签证书默认不校验证书；可用 REKIT_TLS_VERIFY=1 强制校验。"""
    ctx = ssl.create_default_context()
    if (SCHEME == "https" and HOST in ("127.0.0.1", "localhost", "::1")
            and not os.environ.get("REKIT_TLS_VERIFY")):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))


class CliError(Exception):
    pass


def api(method, path, body=None, timeout=120, base=None):
    url = (base or BASE) + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    token = _auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # 本地服务不走系统代理
    opener = _opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error") or detail
        except Exception:
            pass
        raise CliError(f"{e.code} {detail[:300]}")
    except urllib.error.URLError as e:
        raise CliError(f"连不上服务 {BASE}：{e.reason}")
    except socket.timeout:
        raise CliError("请求超时")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def server_alive(timeout=1.5):
    """探测服务是否监听：用公开的 /login 端点。

    鉴权模式下 /api/* 未登录会返回 401，不能用它判断存活（会误判成服务没起）。
    只要 /login 能返回任意 HTTP 响应，就说明服务活着。
    """
    opener = _opener()
    try:
        with opener.open(BASE + "/login", timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500   # 401/403 也算服务活着
    except Exception:
        return False


def ensure_server(quiet=False):
    """服务没起就在后台拉起来，等它可用。"""
    if server_alive():
        return True
    if not quiet:
        hint("服务未运行，正在后台启动…")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    log = open(SERVER_LOG, "a", buffering=1)
    env = dict(os.environ, PORT=str(PORT))
    try:
        p = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "app.py")],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, env=env,
        )
        with open(SERVER_PID, "w") as f:
            f.write(str(p.pid))
    except Exception as e:
        raise CliError(f"启动服务失败：{e}")
    for _ in range(40):
        time.sleep(0.5)
        if server_alive():
            if not quiet:
                ok(f"服务已就绪 {c(BASE, C.GY)}")
            return True
    raise CliError(f"服务启动超时，看日志：{SERVER_LOG}")


def _read_pid():
    try:
        with open(SERVER_PID) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pids_on_port():
    """pid 文件丢了也能把占着端口的进程找出来（macOS/Linux 都靠 lsof）。"""
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}"],
                             capture_output=True, text=True, timeout=5).stdout
        return [int(x) for x in out.split() if x.isdigit()]
    except Exception:
        return []


def stop_server():
    pids = {p for p in (_read_pid(),) if p}
    pids.update(_pids_on_port())
    if not pids:
        hint("服务没在运行")
        return
    import signal
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            err(f"没有权限结束进程 {pid}")
    for _ in range(30):
        if not server_alive(0.4):
            break
        time.sleep(0.3)
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    try:
        os.remove(SERVER_PID)
    except Exception:
        pass
    ok("服务已停止")


# ---------------- SSE ----------------


def sse(exp_id, on_event, stop_on_summary=True, idle_timeout=600, run_id=None):
    """订阅实验事件流。断线自动重连，直到当前 run 结束。

    run_id 必须传：缓冲区按实验存，会重放上一次运行的事件，
    不过滤的话会收到旧的「已完成」而立刻返回。
    """
    url = f"{BASE}/api/experiments/{exp_id}/stream"
    opener = _opener()
    last = time.time()
    retries = 0
    while True:
        if time.time() - last > idle_timeout:
            return "timeout"
        try:
            hdrs = {"Accept": "text/event-stream"}
            token = _auth_token()
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=hdrs)
            with opener.open(req, timeout=idle_timeout) as r:
                retries = 0
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if not line.startswith("data: "):
                        last = time.time()
                        continue
                    last = time.time()
                    try:
                        ev = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    rid = ev.get("run_id")
                    mine = rid is None or run_id is None or rid == run_id
                    if on_event(ev) == "stop":
                        return "stopped"
                    if not mine:
                        continue
                    if ev.get("type") == "status" and ev.get("status") in ("done", "failed"):
                        return "finished"
                    if stop_on_summary and ev.get("type") == "summary":
                        return "finished"
        except (urllib.error.URLError, socket.timeout, ConnectionResetError):
            retries += 1
            if retries > 20:
                return "lost"
            time.sleep(min(2 * retries, 6))


# ---------------- 展示 ----------------


def spark(vals, width=32):
    bars = "▁▂▃▄▅▆▇█"
    v = vals[-width:]
    if not v:
        return ""
    lo, hi = min(v), max(v)
    if hi - lo < 1e-12:
        return bars[3] * len(v)
    return "".join(bars[min(7, int((x - lo) / (hi - lo) * 7))] for x in v)


def fmt_num(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1000:
        return f"{v:.0f}"
    if a >= 1:
        return f"{v:.4f}"
    if a >= 0.001:
        return f"{v:.5f}"
    return f"{v:.3e}"


def fmt_dur(sec):
    if not sec or sec < 0:
        return "—"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


STATUS_TEXT = {
    "draft": ("待开始", C.GY), "planned": ("方案就绪", C.GY), "coded": ("代码就绪", C.GY),
    "running": ("运行中", C.CY), "uploading": ("上传中", C.CY), "setup": ("配置环境", C.CY),
    "pending": ("排队中", C.CY), "done": ("已完成", C.G), "finished": ("已完成", C.G),
    "failed": ("失败", C.RE), "lost": ("失联", C.RE), "stopped": ("已停止", C.Y),
}


def status_str(st):
    label, color = STATUS_TEXT.get(st, (st or "未知", C.GY))
    return c(f"● {label}", color)


def one_line(msg, width=76):
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= width else msg[:width - 1] + "…"


def disp_width(s):
    """粗略计算显示宽度（中文按 2 算）。"""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


# ---------------- 命令 ----------------

def exp_or_die(eid):
    try:
        return api("GET", f"/api/experiments/{eid}")
    except CliError as e:
        if "404" in str(e):
            err(f"实验 #{eid} 不存在")
            sys.exit(1)
        raise


_REQUIRED_PACKAGES = (
    ("flask", "flask"), ("flask_sock", "flask-sock"), ("paramiko", "paramiko"),
    ("cryptography", "cryptography"), ("docx", "python-docx"), ("pypdf", "pypdf"),
    ("requests", "requests"), ("simple_websocket", "simple-websocket"),
)


def _check_dependencies(fix=False):
    """检查运行依赖是否齐全；--fix 时打印可一键执行的安装命令。"""
    import importlib.util
    missing = []
    for module, package in _REQUIRED_PACKAGES:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)
    if not missing:
        ok("Python 依赖齐全")
        return
    err(f"缺少依赖：{' '.join(missing)}")
    req = os.path.join(ROOT, "requirements.txt")
    cmd = f'"{sys.executable}" -m pip install -r "{req}"'
    hint(f"安装命令：{cmd}")
    if fix:
        say(f"  正在安装… {cmd}")
        rc = subprocess.call([sys.executable, "-m", "pip", "install", "-r", req])
        if rc == 0:
            ok("依赖安装完成，建议重启服务：rekit server restart")
        else:
            err("依赖安装失败，请手动执行上面的命令")


def cmd_doctor(args):
    title("环境体检")
    _check_dependencies(fix=getattr(args, "fix", False))
    say()
    ensure_server()
    ok(f"服务 {BASE}")

    s = api("GET", "/api/settings")
    llm = s.get("llm", {})
    if llm.get("api_key") and "*" not in llm.get("api_key", "*"):
        r = api("POST", "/api/llm/test")
        if r.get("ok"):
            ok(f"大模型可用：{llm.get('model')} @ {llm.get('base_url')}")
        else:
            err(f"大模型不可用：{one_line(r.get('error'), 100)}")
    else:
        hint("大模型未配置（只能用占位方案和占位代码）→ rekit config --edit")

    hosts = api("GET", "/api/hosts")
    if not hosts:
        hint("还没有服务器 → rekit host add")
    for h in hosts:
        say()
        r = api("POST", f"/api/hosts/{h['id']}/test")
        if r.get("ok"):
            info = r["info"]
            ok(f"{h['name']} {h['username']}@{h['host']}:{h['port']}  "
               f"{info.get('os','')[:28]} | {info.get('python','').strip()[:20]}")
            if info.get("has_gpu"):
                hint(f"GPU: {info.get('gpu','').strip()[:60]}")
        else:
            err(f"{h['name']} 连接失败：{one_line(r.get('error'), 90)}")


def cmd_list(args):
    ensure_server()
    exps = api("GET", "/api/experiments")
    if not exps:
        hint("还没有实验 → rekit new")
        return
    title("实验列表")
    for e in exps:
        say(f"  {c('#' + str(e['id']), C.GY).ljust(6)} {status_str(e['status'])}  "
            f"{one_line(e['title'], 48)}  {c(str(e['run_count']) + ' 次运行', C.GY)}")


def cmd_show(args):
    ensure_server()
    d = exp_or_die(args.id)
    p = d.get("plan")
    title(f"#{d['id']}  {d['title']}")
    say(f"  状态 {status_str(d['status'])}")
    rule()

    say(c("创新点", C.GY))
    say("  " + "\n  ".join(textwrap_wrap(d["innovation"], 72)))

    if not p:
        hint("还没有方案 → rekit plan " + str(d["id"]))
        return

    say()
    say(c("实验方案", C.GY))
    say(f"  核心命题  {p.get('core_claim','')}")
    if p.get("hypothesis"):
        say("  假设")
        for h in p["hypothesis"]:
            say(f"    · {h}")
    if p.get("metrics"):
        say("  指标      " + "、".join(
            f"{m['name']}{'↑' if m.get('higher_better', True) else '↓'}" for m in p["metrics"]))
    if p.get("baselines"):
        say("  基线      " + "、".join(p["baselines"]))
    if p.get("ablations"):
        say("  消融      " + "、".join(p["ablations"]))

    code = d.get("code")
    if code:
        say()
        say(c("代码", C.GY) + f"  {len(code.get('files', []))} 个文件: "
            + ", ".join(f["path"] for f in code.get("files", [])))

    run = d.get("latest_run")
    if run:
        say()
        say(c("最近运行", C.GY))
        say(f"  run #{run['id']}  {status_str(run['status'])}  "
            f"exit={run.get('exit_code') or '—'}  "
            f"耗时 {fmt_dur((run.get('finished_at') or time.time()) - (run.get('started_at') or time.time()))}")

    if d.get("summary"):
        say()
        print_summary(d["summary"])


def textwrap_wrap(s, width):
    out, cur = [], ""
    for ch in s:
        cur += ch
        if disp_width(cur) >= width - 2 or ch == "\n":
            out.append(cur.rstrip("\n"))
            cur = ""
    if cur:
        out.append(cur)
    return out


def read_idea(arg):
    """-i @文件 时读本地文稿；.docx / .pdf 也能读（复用 core.extract）。"""
    path = os.path.expanduser(arg[1:])
    if not os.path.isfile(path):
        err(f"找不到文件：{path}")
        sys.exit(1)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from core import extract
        text = extract.extract_path(path)
    except Exception as e:
        try:                                  # 退化成纯文本读
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            err(f"读取失败：{e}")
            sys.exit(1)
    hint(f"已从 {path} 读取创新点（{len(text)} 字）")
    return text


def cmd_new(args):
    ensure_server()
    title_new = args.title
    innovation = args.innovation

    if innovation and innovation.startswith("@"):
        innovation = read_idea(innovation)

    if not title_new:
        title_new = input(c("实验标题：", C.B)).strip()
    if not innovation:
        say(c("写下你的创新点（写完按 Ctrl-D 结束，或先写到文件用 -i @文件）", C.GY))
        lines = []
        try:
            while True:
                lines.append(input())
        except EOFError:
            pass
        innovation = "\n".join(lines).strip()

    if len(innovation) < 10:
        err("创新点太短了，至少写 10 个字")
        sys.exit(1)

    r = api("POST", "/api/experiments",
            {"title": title_new or "未命名实验", "innovation": innovation})
    ok(f"实验 #{r['id']} 已创建")
    hint(f"下一步：rekit plan {r['id']}")


def _wait_job(eid, kind, label, timeout=300):
    """等后台 job 完成，边等边转圈；一旦事件流里出现错误就立刻退出。"""
    t0 = time.time()
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while time.time() - t0 < timeout:
        d = api("GET", f"/api/experiments/{eid}")
        done = (kind == "plan" and d.get("plan")) or (kind == "code" and d.get("code"))
        if done:
            say(f"\r  {c('✓', C.G)} {label}完成{' ' * 24}")
            return d["plan"] if kind == "plan" else d["code"]

        for ev in reversed(d.get("events") or []):
            if ev.get("level") == "error" and ev.get("ts", 0) > t0 - 5:
                say("")
                raise CliError(one_line(ev.get("message"), 200))
        if sys.stdout.isatty():
            sys.stdout.write(f"\r  {c(frames[i % 10], C.CY)} {label}中… {int(time.time()-t0)}s ")
            sys.stdout.flush()
        i += 1
        time.sleep(1.2)
    say("")
    raise CliError(f"{label}超时，去网页看看发生了什么：{BASE}/exp/{eid}")


def cmd_plan(args):
    ensure_server()
    d = exp_or_die(args.id)
    api("POST", f"/api/experiments/{args.id}/plan", {"feedback": args.feedback or ""})
    p = _wait_job(args.id, "plan", "生成方案")
    say()
    say(f"  {c(p['title'], C.B)}")
    say(f"  {one_line(p.get('core_claim'), 72)}")
    if p.get("hypothesis"):
        for h in p["hypothesis"]:
            say(f"   · {one_line(h, 70)}")
    say()
    hint(f"下一步：rekit code {args.id}")


def cmd_code(args):
    ensure_server()
    exp_or_die(args.id)
    api("POST", f"/api/experiments/{args.id}/code", {"feedback": args.feedback or ""})
    code = _wait_job(args.id, "code", "编写代码")
    say()
    for f in code.get("files", []):
        n = len(f.get("content", "").split("\n"))
        say(f"  {c('▸', C.CY)} {f['path']}  {c(f'{n} 行', C.GY)}")
    if code.get("notes"):
        say()
        hint(one_line(code["notes"], 74))
    say()
    hint(f"下一步：rekit run {args.id} --follow")


def cmd_run(args):
    ensure_server()
    d = exp_or_die(args.id)
    eid = args.id

    if not d.get("plan"):
        hint("还没有方案，先生成")
        cmd_plan(argparse.Namespace(id=eid, feedback=None))
    d = exp_or_die(eid)
    if not d.get("code"):
        hint("还没有代码，先生成")
        cmd_code(argparse.Namespace(id=eid, feedback=None))

    host_id = args.host
    hosts = api("GET", "/api/hosts")
    if not hosts:
        err("还没有服务器 → rekit host add")
        sys.exit(1)
    if host_id is None:
        if len(hosts) == 1:
            host_id = hosts[0]["id"]
        else:
            title("选择服务器")
            for i, h in enumerate(hosts, 1):
                say(f"  [{i}] {h['name']}  {h['username']}@{h['host']}:{h['port']}")
            try:
                pick = input(c("序号：", C.B)).strip()
            except EOFError:
                pick = "1"
            host_id = hosts[int(pick or 1) - 1]["id"]
    host = next((h for h in hosts if h["id"] == host_id), None)
    if not host:
        err("主机不存在")
        sys.exit(1)

    say()
    say(f"  {c('▸', C.CY)} 部署到 {c(host['name'], C.B)} ({host['username']}@{host['host']})")
    r = api("POST", f"/api/experiments/{eid}/run", {"host_id": host_id})
    run_id = r["run_id"]
    say(f"  {c('▸', C.CY)} run #{run_id} 已排队")
    say()

    if args.detach:
        ok("已提交，后台运行中")
        hint(f"查看进度：rekit logs {eid} -f    /    rekit watch {eid}")
        return

    follow(eid, run_id, show_metrics=not args.no_metrics)


def follow(eid, run_id, show_metrics=True):
    """实时跟随日志直到结束，然后打印总结。"""
    series = {}
    state = {"stage": "启动中", "t0": time.time(), "exit": None}
    last_draw = 0.0
    metrics_mode = show_metrics

    def draw_status(force=False):
        nonlocal last_draw
        if not sys.stdout.isatty():
            return
        now = time.time()
        if not force and now - last_draw < 0.5:
            return
        last_draw = now
        el = fmt_dur(now - state["t0"])
        tail = ""
        if series:
            name = max(series, key=lambda k: len(series[k]))
            vals = [p["v"] for p in series[name][-1:]]
            tail = f"  {name}={fmt_num(vals[0])}" if vals else ""
        line = f"\r  {c('●', C.CY)} {state['stage']}  {c(el, C.GY)}{tail}          "
        sys.stdout.write(line)
        sys.stdout.flush()

    def on_event(ev):
        # SSE 的缓冲区按实验存，会重放上一次运行的日志，这里按 run_id 过滤
        rid = ev.get("run_id")
        if rid is not None and rid != run_id:
            return None
        t = ev.get("type")
        if t == "log":
            line = ev.get("line", "")
            if line.startswith("@@METRIC"):
                if metrics_mode:
                    kv = dict(p.split("=", 1) for p in line[8:].split() if "=" in p)
                    name = kv.get("name", "?")
                    try:
                        val = float(kv.get("value", "nan"))
                    except ValueError:
                        return None
                    series.setdefault(name, []).append({"v": val})
                    if sys.stdout.isatty():
                        say(f"\r  {c('·', C.GY)} {name:<18}{c(fmt_num(val), C.B)}"
                            f"  {c(spark([p['v'] for p in series[name]]), C.G)}   ")
                    else:
                        say(f"{name}={fmt_num(val)}")
                    return None
                return None
            if not sys.stdout.isatty() and not line.strip():
                return None
            say(f"  {c('│', C.GY)} {line}")
        elif t == "note":
            lvl = ev.get("level")
            mark = c("!", C.Y) if lvl == "warn" else (c("✕", C.RE) if lvl == "error" else c("▸", C.CY))
            say(f"  {mark} {ev.get('message','')}")
            state["stage"] = one_line(ev.get("message", ""), 30)
        elif t == "status":
            st = ev.get("status")
            state["stage"] = STATUS_TEXT.get(st, (st, ""))[0]
            if st in ("setup", "running", "uploading"):
                state["t0"] = time.time() if st == "running" else state["t0"]
        elif t == "summary":
            return "stop"
        return None

    say(c("实时日志", C.GY))
    try:
        result = sse(eid, on_event, run_id=run_id)
    except KeyboardInterrupt:
        say("")
        hint("已断开跟随（实验仍在服务器上跑）")
        return
    say("")
    if result == "stopped":
        time.sleep(0.4)

    d = exp_or_die(eid)
    run = None
    for r in d.get("runs", []):
        if r["id"] == run_id:
            run = r
            break
    say()
    if run:
        st = run["status"]
        dur = fmt_dur((run.get("finished_at") or time.time()) - (run.get("started_at") or time.time()))
        say(f"  {status_str(st)}  exit={run.get('exit_code') or '—'}  {c(dur, C.GY)}")
    if d.get("summary"):
        say()
        print_summary(d["summary"])


def print_summary(s):
    if s.get("error"):
        err(f"总结失败：{s['error']}")
        return
    rule()
    say(c("实验结论", C.B))
    v = s.get("verdict", "")
    color = C.G if v.startswith("支持") else (C.RE if v.startswith("不支持") else C.Y)
    say(f"  {c(v, color)}  {s.get('one_liner','')}")
    say()

    for h in s.get("hypothesis_results") or []:
        tag = {"supported": c("支持", C.G), "refuted": c("不支持", C.RE)}.get(
            h.get("verdict"), c("不确定", C.Y))
        say(f"  [{tag}] {one_line(h.get('hypothesis'), 60)}")
        if h.get("evidence"):
            say(f"        {c(one_line(h['evidence'], 64), C.GY)}")

    stats = s.get("metrics_stats") or []
    if stats:
        say()
        say(c("指标", C.GY))
        for m in stats:
            arrow = "↑" if (m.get("change_pct") or 0) > 0 else "↓"
            say(f"  {m['name']:<20} {fmt_num(m['first']):>10} {arrow} {fmt_num(m['last']):>10}"
                f"   最优 {c(fmt_num(m['best']), C.B)}  {c(str(m.get('points', 0)) + ' 点', C.GY)}")

    for label, key, color in (("关键发现", "key_findings", C.CY),
                              ("异常", "anomalies", C.Y),
                              ("局限", "limitations", C.GY),
                              ("下一步", "next_steps", C.CY)):
        items = s.get(key) or []
        if not items:
            continue
        say()
        say(c(label, C.GY))
        for x in items[:6]:
            say(f"  · {one_line(x, 70)}")

    if s.get("paper_paragraph") and not s["paper_paragraph"].startswith("（"):
        say()
        say(c("可直接进论文的段落", C.GY))
        for ln in textwrap_wrap("  " + s["paper_paragraph"], 72):
            say("  " + c(ln, C.D))


def cmd_logs(args):
    ensure_server()
    d = exp_or_die(args.id)
    run = d.get("latest_run")
    if not run:
        hint("还没有运行记录")
        return

    tail = [x for x in (run.get("log_tail") or "").split("\n") if x]

    if not args.follow:
        if not tail:
            hint("没有日志")
        for line in tail[-args.n:]:
            say(f"  {c('│', C.GY)} {line}")
        return

    # 跟随模式：只打几行上下文，剩下的靠事件流补（避免整段重复）
    for line in tail[-5:]:
        say(f"  {c('│', C.GY)} {line}")
    say(c("— 继续跟随 —", C.GY))

    run_id = run["id"]

    def on_event(ev):
        rid = ev.get("run_id")
        if rid is not None and rid != run_id:
            return None
        if ev.get("type") == "log":
            line = ev.get("line", "")
            if not line.startswith("@@METRIC"):
                say(f"  {c('│', C.GY)} {line}")
        elif ev.get("type") == "note":
            say(f"  {c('▸', C.CY)} {ev.get('message', '')}")
        elif ev.get("type") == "status" and ev.get("status") in ("done", "failed"):
            return "stop"
        return None

    try:
        sse(args.id, on_event, run_id=run_id)
    except KeyboardInterrupt:
        say("")
        hint("已断开")


def cmd_watch(args):
    ensure_server()
    d = exp_or_die(args.id)
    run = d.get("latest_run")
    if not run:
        hint("还没有运行记录")
        return

    series = {}
    t0 = run.get("started_at") or time.time()

    def fetch():
        m = api("GET", f"/api/experiments/{args.id}/metrics?run_id={run['id']}")
        return m.get("series") or {}

    def render():
        cols = min(shutil.get_terminal_size((80, 20)).columns - 4, 76)
        lines = []
        el = fmt_dur((run.get("finished_at") or time.time()) - t0)
        lines.append(f"  {status_str(run['status'])}  run #{run['id']}  {c(el, C.GY)}")
        for name, pts in sorted(series.items()):
            vals = [p["v"] for p in pts]
            if not vals:
                continue
            lines.append(f"  {c(name.ljust(16), C.B)} {fmt_num(vals[-1]):>10}  "
                         f"{c(spark(vals, cols - 34), C.G)}")
        return "\n".join(lines)

    try:
        while True:
            series = fetch()
            out = render()
            if sys.stdout.isatty():
                sys.stdout.write("\033[2J\033[H")
            say(out)
            rd = exp_or_die(args.id)["latest_run"]
            if rd["status"] in ("finished", "failed", "lost", "stopped"):
                say()
                ok(f"实验结束：{rd['status']} exit={rd.get('exit_code') or '—'}")
                return
            time.sleep(2)
    except KeyboardInterrupt:
        say("")
        hint("已停止刷新")


def cmd_report(args):
    ensure_server()
    d = exp_or_die(args.id)
    s = d.get("summary")
    if not s:
        hint("还没有总结。实验跑完会自动生成；也可以手动：rekit report 前先让实验结束")
        hint("或者直接调用：POST /api/runs/<id>/summarize")
        return
    title(f"#{d['id']} {d['title']}")
    print_summary(s)

    if args.markdown:
        path = args.markdown
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_markdown(d, s))
        ok(f"已导出 {path}")


def to_markdown(d, s):
    p = d.get("plan") or {}
    L = [f"# {d['title']}", "",
         f"> {s.get('verdict','')} —— {s.get('one_liner','')}", "",
         "## 核心命题", p.get("core_claim", ""), "", "## 可检验假设"]
    L += [f"- {h}" for h in p.get("hypothesis", [])] or ["- （无）"]
    L += ["", "## 假设检验结果"]
    for h in s.get("hypothesis_results") or []:
        L.append(f"- **{h.get('hypothesis','')}** → `{h.get('verdict','')}`")
        if h.get("evidence"):
            L.append(f"  - {h['evidence']}")
    L += ["", "## 关键发现"]
    L += [f"- {x}" for x in s.get("key_findings") or []]
    if s.get("comparison"):
        L += ["", "## 对比结论", s["comparison"]]
    stats = s.get("metrics_stats") or []
    if stats:
        L += ["", "## 指标汇总", "| 指标 | 点数 | 起始 | 结束 | 最优 |", "|---|---|---|---|---|"]
        for m in stats:
            L.append(f"| {m['name']} | {m['points']} | {m['first']} | {m['last']} | {m['best']} |")
    for label, key in (("异常", "anomalies"), ("局限", "limitations"), ("下一步", "next_steps")):
        items = s.get(key) or []
        if items:
            L += ["", f"## {label}"] + [f"- {x}" for x in items]
    if s.get("paper_paragraph"):
        L += ["", "## 论文段落", s["paper_paragraph"]]
    L += ["", "---", f"运行状态：{s.get('run_status','')} | 耗时：{s.get('duration','')}"]
    return "\n".join(L) + "\n"


def cmd_stop(args):
    ensure_server()
    d = exp_or_die(args.id)
    run = d.get("latest_run")
    if not run:
        hint("没有可停止的运行")
        return
    if not args.yes:
        try:
            if input(c(f"确认停止 run #{run['id']}？[y/N] ", C.B)).strip().lower() != "y":
                return
        except EOFError:
            return
    api("POST", f"/api/runs/{run['id']}/stop")
    ok("已发送停止信号")


def cmd_host_list(args):
    ensure_server()
    hosts = api("GET", "/api/hosts")
    if not hosts:
        hint("还没有服务器 → rekit host add -a 1.2.3.4 -u root")
        return
    title("服务器")
    for h in hosts:
        gpu = c(" · GPU", C.G) if h.get("gpu") else ""
        say(f"  {c('#' + str(h['id']), C.GY).ljust(6)} {h['name']:<14} "
            f"{h['username']}@{h['host']}:{h['port']}{gpu}")
        say(f"         {c(h.get('workdir', ''), C.GY)}   {c(h.get('python_bin', ''), C.GY)}")


def cmd_host_add(args):
    ensure_server()
    title("添加服务器")
    d = {
        "name": args.name or "server",
        "host": args.address,
        "port": args.port or 22,
        "username": args.user,
        "auth_type": "key" if args.key else "password",
        "key_path": args.key or "",
        "password": args.password or "",
        "workdir": args.workdir or "~/research-runs",
        "python_bin": args.python or "python3",
        "gpu": 1 if args.gpu else 0,
    }
    if not d["host"] or not d["username"]:
        err("需要 -a 地址 和 -u 用户名")
        sys.exit(1)
    if d["auth_type"] == "password" and not d["password"]:
        import getpass
        d["password"] = getpass.getpass(c("密码：", C.B))
    r = api("POST", "/api/hosts", d)
    ok(f"已添加 #{r['id']}")
    t = api("POST", f"/api/hosts/{r['id']}/test")
    if t.get("ok"):
        info = t["info"]
        ok(f"连接成功：{info.get('os', '')[:30]} | {info.get('python', '').strip()[:24]}")
        if info.get("has_gpu"):
            hint(f"GPU: {info.get('gpu', '').strip()[:60]}")
    else:
        err(f"连接失败：{one_line(t.get('error'), 90)}")


def cmd_host_rm(args):
    ensure_server()
    hosts = api("GET", "/api/hosts")
    h = next((x for x in hosts if x["id"] == args.id), None)
    if not h:
        err("主机不存在")
        sys.exit(1)
    api("DELETE", f"/api/hosts/{args.id}")
    ok(f"已删除 {h['name']}")


def cmd_host_test(args):
    ensure_server()
    hosts = api("GET", "/api/hosts")
    target = hosts if args.id is None else [h for h in hosts if h["id"] == args.id]
    if not target:
        hint("没有可测的服务器")
        return
    title("连接测试")
    for h in target:
        r = api("POST", f"/api/hosts/{h['id']}/test")
        if r.get("ok"):
            info = r["info"]
            ok(f"{h['name']}：{info.get('os', '')[:30]} | {info.get('python', '').strip()[:24]}")
            if info.get("has_gpu"):
                hint(f"GPU: {info.get('gpu', '').strip()[:70]}")
            else:
                hint("无 GPU 或未装 nvidia-smi")
        else:
            err(f"{h['name']}：{one_line(r.get('error'), 90)}")


def cmd_server(args):
    act = getattr(args, "server_cmd", None) or "status"

    if act == "start":
        ensure_server()
        return
    if act == "stop":
        stop_server()
        return
    if act == "restart":
        say(c("重启服务", C.B))
        stop_server()
        time.sleep(0.6)
        ensure_server()
        return
    if act == "logs":
        if not os.path.exists(SERVER_LOG):
            hint("还没有日志")
            return
        with open(SERVER_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for ln in lines[-(args.n or 40):]:
            say("  " + ln.rstrip())
        if not args.follow:
            return
        pos = os.path.getsize(SERVER_LOG)
        say(c("— 继续跟随 —", C.GY))
        try:
            while True:
                time.sleep(0.6)
                size = os.path.getsize(SERVER_LOG)
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(SERVER_LOG, encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        for ln in f:
                            say("  " + ln.rstrip())
                        pos = f.tell()
        except KeyboardInterrupt:
            say("")
        return

    alive = server_alive()
    pid = _read_pid()
    title("服务状态")
    if alive:
        ok(f"运行中  {c(BASE, C.GY)}" + (f"  pid={pid}" if pid else ""))
        try:
            exps = api("GET", "/api/experiments")
            running = [e for e in exps if e["status"] == "running"]
            say(f"     实验 {len(exps)} 个" + (f"，{len(running)} 个在跑" if running else ""))
        except CliError:
            pass
    else:
        warn("未运行（任何命令都会自动拉起）")
    say(f"     {c('日志', C.GY)}  {SERVER_LOG}")
    say()
    hint("rekit server start / stop / restart / logs -f")


def cmd_login(args):
    """登录并保存令牌到本地 data/cli_token。"""
    import getpass
    ensure_server()
    username = args.username or input("用户名：").strip()
    password = args.password or getpass.getpass("密码：")
    try:
        r = api("POST", "/api/auth/login", body={"username": username, "password": password})
    except CliError as e:
        err(str(e))
        hint("首次使用请先在网页 /login 创建管理员账号")
        return
    _save_token(r["token"])
    ok(f"已登录：{r['user']['username']}（{r['user']['role']}）")
    hint("令牌已保存到 data/cli_token，后续命令无需重复登录")


def cmd_logout(args):
    _clear_token()
    ok("已退出（本地令牌已删除）")


def cmd_whoami(args):
    ensure_server()
    try:
        u = api("GET", "/api/auth/me")
        ok(f"{u['username']}（{u['role']}）· id={u['id']}")
    except CliError as e:
        err(str(e))
        hint("先执行 rekit login")


def cmd_admin_password(args):
    pw = auth.get_initial_admin_password()
    if not pw:
        hint("没有可用的初始密码（可能未生成或已设置 REKIT_ADMIN_PASSWORD）")
        return
    say(pw)
    hint("这是 admin 的初始密码；若已改密则以新密码为准")


def cmd_token(args):
    """登录状态下生成新令牌，打印出来供环境变量/CI 使用。"""
    ensure_server()
    try:
        r = api("POST", "/api/users/me/token", body={})
        say(r["token"])
        hint("令牌仅显示一次，请妥善保存；生成后旧令牌立即失效")
    except CliError as e:
        err(str(e))


def cmd_config(args):
    ensure_server()
    cfg_path = os.path.join(ROOT, "data", "config.json")

    if args.set_key:
        api("POST", "/api/settings", {"llm": {"api_key": args.set_key}})
        ok("API Key 已写入")
        r = api("POST", "/api/llm/test")
        if r.get("ok"):
            ok(f"连通性验证通过：{r.get('reply', '')[:20]}")
        else:
            warn(f"保存了，但测试失败：{one_line(r.get('error'), 90)}")
        return

    if args.edit:
        # 文件不存在就先落一份完整默认配置，避免编辑器打开空文件
        if not os.path.exists(cfg_path):
            cfg = api("GET", "/api/settings")
            cfg.setdefault("llm", {})["api_key"] = ""
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        editor = os.environ.get("EDITOR") or "vi"
        subprocess.call([editor, cfg_path])
        ok("已保存，重启服务后生效（或直接在网页「设置」页改）")
        return

    cfg = api("GET", "/api/settings")
    llm = cfg.get("llm", {})
    key = llm.get("api_key") or ""
    if not key:
        masked = c("未配置", C.Y)
    elif "*" in key:
        masked = c(key, C.GY) + c("（已配置）", C.G)
    else:
        masked = c("已配置", C.G)

    title("配置")
    say(f"  {c('接口', C.GY)}      {llm.get('base_url', '')}")
    say(f"  {c('模型', C.GY)}      {llm.get('model', '')}")
    say(f"  {c('API Key', C.GY)}   {masked}")
    say(f"  {c('轮询间隔', C.GY)}  {cfg.get('monitor', {}).get('interval_sec')} 秒")
    say(f"  {c('配置文件', C.GY)}  {cfg_path}")
    say()
    hint("完整编辑：rekit config --edit")
    hint("只换 Key：rekit config --set-key sk-xxx")


def cmd_open(args):
    ensure_server()
    url = f"{BASE}/exp/{args.id}" if args.id else BASE
    opener = "open" if sys.platform == "darwin" else ("start" if os.name == "nt" else "xdg-open")
    try:
        subprocess.Popen([opener, url])
        ok(f"已在浏览器打开 {url}")
    except Exception:
        hint(url)


def cmd_auto(args):
    """一条命令：创新点 → 方案 → 代码 → 部署 → 跟随 → 报告。"""
    ensure_server()
    innovation = args.innovation
    if innovation and innovation.startswith("@"):
        innovation = read_idea(innovation)
    if not innovation:
        err("需要创新点：rekit auto -i \"你的想法\" 或 -i @想法.md")
        sys.exit(1)
    if len(innovation) < 10:
        err("创新点太短")
        sys.exit(1)

    t_start = time.time()
    title("① 创建实验")
    r = api("POST", "/api/experiments", {"title": args.title or "自动实验", "innovation": innovation})
    eid = r["id"]
    ok(f"实验 #{eid}")

    title("② 生成方案")
    api("POST", f"/api/experiments/{eid}/plan", {"feedback": ""})
    p = _wait_job(eid, "plan", "生成方案")
    say(f"  {c(p['title'], C.B)}")
    say(f"  {one_line(p.get('core_claim'), 70)}")

    title("③ 编写代码")
    api("POST", f"/api/experiments/{eid}/code", {"feedback": ""})
    code = _wait_job(eid, "code", "编写代码")
    for f in code.get("files", []):
        say(f"  {c('▸', C.CY)} {f['path']}  {c(str(len(f.get('content','').split(chr(10)))) + ' 行', C.GY)}")

    title("④ 部署运行")
    hosts = api("GET", "/api/hosts")
    if not hosts:
        err("没有服务器 → rekit host add")
        sys.exit(1)
    host_id = args.host or hosts[0]["id"]
    host = next((h for h in hosts if h["id"] == host_id), hosts[0])
    say(f"  {c('▸', C.CY)} {host['name']} ({host['username']}@{host['host']})")
    rr = api("POST", f"/api/experiments/{eid}/run", {"host_id": host["id"]})
    run_id = rr["run_id"]
    say(f"  {c('▸', C.CY)} run #{run_id}")
    say()

    title("⑤ 实时监控")
    follow(eid, run_id)

    say()
    rule()
    ok(f"全流程耗时 {fmt_dur(time.time() - t_start)}   实验 #{eid}")
    hint(f"网页查看：{BASE}/exp/{eid}")
    hint(f"导出报告：rekit report {eid} --markdown 报告.md")


# ---------------- 入口 ----------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="rekit",
        description="科研实验助手 —— 从创新点到实验报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  rekit auto -i 想法.md              一条命令跑完整个实验
  rekit new -t 标题 -i 想法          手动建实验
  rekit run 3 -H 2                   部署并实时跟随日志
  rekit report 3 --markdown 报.md    导出报告
  rekit doctor                       体检服务/大模型/服务器""",
    )
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("login", help="登录并保存令牌（鉴权模式下必做）")
    sp.add_argument("-u", "--username")
    sp.add_argument("-p", "--password")
    sp.set_defaults(fn=cmd_login)

    sub.add_parser("logout", help="退出（删除本地令牌）").set_defaults(fn=cmd_logout)
    sub.add_parser("whoami", help="查看当前登录账号").set_defaults(fn=cmd_whoami)
    sub.add_parser("admin-password", help="查看解密后的 admin 初始密码").set_defaults(fn=cmd_admin_password)
    sub.add_parser("token", help="生成新的命令行令牌").set_defaults(fn=cmd_token)

    sp = sub.add_parser("doctor", help="体检：依赖、服务、大模型、服务器")
    sp.add_argument("--fix", action="store_true", help="自动安装缺失的 Python 依赖")
    sp.set_defaults(fn=cmd_doctor)
    sub.add_parser("list", help="实验列表").set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="查看实验详情")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("new", help="新建实验")
    sp.add_argument("-t", "--title")
    sp.add_argument("-i", "--innovation", help="创新点，或 @文件路径")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("plan", aliases=["p"], help="生成实验方案")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--feedback", help="修改意见")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("code", aliases=["c"], help="生成实验代码")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--feedback", help="修改意见")
    sp.set_defaults(fn=cmd_code)

    sp = sub.add_parser("run", aliases=["r"], help="部署并运行实验")
    sp.add_argument("id", type=int)
    sp.add_argument("-H", "--host", type=int, help="主机 id")
    sp.add_argument("--detach", action="store_true", help="提交后立即返回")
    sp.add_argument("--no-metrics", action="store_true", help="不显示指标曲线行")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("logs", aliases=["l"], help="查看/跟随日志")
    sp.add_argument("id", type=int)
    sp.add_argument("-f", "--follow", action="store_true")
    sp.add_argument("-n", type=int, default=30)
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("watch", aliases=["w"], help="实时刷新指标曲线")
    sp.add_argument("id", type=int)
    sp.set_defaults(fn=cmd_watch)

    sp = sub.add_parser("report", help="打印/导出实验报告")
    sp.add_argument("id", type=int)
    sp.add_argument("--markdown", metavar="文件")
    sp.set_defaults(fn=cmd_report)

    sp = sub.add_parser("stop", help="停止正在跑的实验")
    sp.add_argument("id", type=int)
    sp.add_argument("-y", "--yes", action="store_true")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("auto", help="一条命令：想法 → 报告")
    sp.add_argument("-i", "--innovation", help="创新点，或 @文件路径")
    sp.add_argument("-t", "--title")
    sp.add_argument("-H", "--host", type=int)
    sp.set_defaults(fn=cmd_auto)

    sp = sub.add_parser("host", help="服务器管理")
    hsub = sp.add_subparsers(dest="host_cmd")
    sp.set_defaults(fn=cmd_host_list)

    h = hsub.add_parser("list", help="列出服务器")
    h.set_defaults(fn=cmd_host_list)

    h = hsub.add_parser("add", help="添加服务器")
    h.add_argument("-a", "--address", required=True, help="地址")
    h.add_argument("-u", "--user", required=True, help="用户名")
    h.add_argument("-p", "--port", type=int, default=22)
    h.add_argument("-n", "--name", help="显示名")
    h.add_argument("--password", help="密码（不填会交互输入）")
    h.add_argument("--key", help="私钥路径，用密钥登录")
    h.add_argument("--workdir", help="远程工作目录，默认 ~/research-runs")
    h.add_argument("--python", help="远程 python，如 python3.11")
    h.add_argument("--gpu", action="store_true", help="采集 GPU 状态")
    h.set_defaults(fn=cmd_host_add)

    h = hsub.add_parser("rm", help="删除服务器")
    h.add_argument("id", type=int)
    h.set_defaults(fn=cmd_host_rm)

    h = hsub.add_parser("test", help="测试连接")
    h.add_argument("id", type=int, nargs="?")
    h.set_defaults(fn=cmd_host_test)

    sp = sub.add_parser("server", help="服务管理（start/stop/restart/logs）")
    ssub = sp.add_subparsers(dest="server_cmd")
    sp.set_defaults(fn=cmd_server)
    ssub.add_parser("status", help="查看状态").set_defaults(fn=cmd_server)
    ssub.add_parser("start", help="启动").set_defaults(fn=cmd_server)
    ssub.add_parser("stop", help="停止").set_defaults(fn=cmd_server)
    ssub.add_parser("restart", help="重启").set_defaults(fn=cmd_server)
    sl = ssub.add_parser("logs", help="看服务日志")
    sl.add_argument("-n", type=int, default=40)
    sl.add_argument("-f", "--follow", action="store_true")
    sl.set_defaults(fn=cmd_server)

    sp = sub.add_parser("config", help="查看/修改配置")
    sp.add_argument("--edit", action="store_true")
    sp.add_argument("--set-key", metavar="KEY")
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("open", help="在浏览器打开")
    sp.add_argument("id", type=int, nargs="?")
    sp.set_defaults(fn=cmd_open)

    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        # 无参数：概览
        ensure_server()
        title("科研实验助手")
        exps = api("GET", "/api/experiments")
        hosts = api("GET", "/api/hosts")
        s = api("GET", "/api/settings")
        configured = bool(s.get("llm", {}).get("api_key")) and "*" not in s["llm"]["api_key"]
        say(f"  服务     {c(BASE, C.GY)}")
        say(f"  大模型   {c('已配置', C.G) if configured else c('未配置', C.Y)}")
        say(f"  服务器   {len(hosts)} 台")
        say(f"  实验     {len(exps)} 个")
        if exps:
            say()
            for e in exps[:5]:
                say(f"  {c('#' + str(e['id']), C.GY).ljust(6)} {status_str(e['status'])}  "
                    f"{one_line(e['title'], 46)}")
        say()
        hint("rekit doctor            体检")
        hint("rekit auto -i 想法.md   一条命令跑完")
        hint("rekit --help            全部命令")
        return 0

    try:
        args.fn(args)
        return 0
    except CliError as e:
        say()
        err(str(e))
        return 1
    except KeyboardInterrupt:
        say("")
        hint("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
