"""SSH 交互式终端会话：把一个 paramiko shell 通道桥接成可推给前端的字节流。

注意并发：simple-websocket 底层的 wsproto 不是线程安全的，send 和 receive 必须
在同一个线程里串行调用。所以这里的会话只负责"收字节进队列"，由 WebSocket 处理
函数单线程轮转读取，不在这里反向调用 ws。

另一个坑：很多云主机（AutoDL 等）默认 LANG 为空、LC_CTYPE=POSIX，`ls` 碰到中文名
会用 $'\\345\\256\\236...' 转义，看着像"中文显示不出来"。所以连接后先探一次远端
locale，能注入就通过 SSH env 请求注入 UTF-8（sshd 需 AcceptEnv LANG LC_*）。
"""
import queue
import socket
import threading
import time

from core import db, ssh_runner

# 优先级从高到低，取远端实际装了的那个
UTF8_LOCALES = ["C.UTF-8", "en_US.UTF-8", "zh_CN.UTF-8",
                "C.utf8", "en_US.utf8", "zh_CN.utf8", "UTF-8"]

# 同一台主机的 locale 探测结果复用，避免每次开终端都多一个往返
_LOCALE_CACHE = {}


def _is_utf8(value):
    v = (value or "").strip().lower()
    return "utf-8" in v or "utf8" in v


def detect_locale(client, cache_key=None):
    """探测远端 locale。返回需要注入的环境变量；已经是对的或探测失败则返回 {}。"""
    if cache_key is not None and cache_key in _LOCALE_CACHE:
        return _LOCALE_CACHE[cache_key]

    env = {}
    try:
        chan = client.get_transport().open_session()
        chan.settimeout(3)
        chan.exec_command('printf "%s|%s\\n" "$LANG" "$LC_ALL"; locale -a 2>/dev/null')
        buf = b""
        deadline = time.time() + 3
        while time.time() < deadline:
            if chan.recv_ready():
                buf += chan.recv(65536)
                continue
            if chan.exit_status_ready():
                while chan.recv_ready():
                    buf += chan.recv(65536)
                break
            time.sleep(0.02)
        chan.close()

        text = buf.decode("utf-8", "replace")
        first, _, rest = text.partition("\n")
        current = [x for x in first.strip().split("|") if x.strip()]
        # 远端已经是 UTF-8 就别多管闲事
        if not any(_is_utf8(x) for x in current):
            available = {ln.strip() for ln in rest.splitlines() if ln.strip()}
            for want in UTF8_LOCALES:
                if want in available:
                    env = {"LANG": want, "LC_ALL": want}
                    break
    except Exception:
        env = {}

    if cache_key is not None:
        _LOCALE_CACHE[cache_key] = env
    return env


class TerminalSession:
    def __init__(self, host_id, cols=120, rows=32):
        self.host = db.get_host(host_id, include_secrets=True)
        if not self.host:
            raise ValueError(f"主机 #{host_id} 不存在")
        self.cols = max(10, int(cols))
        self.rows = max(5, int(rows))
        self.locale_env = {}
        self.client = ssh_runner.connect(self.host, timeout=15)
        try:
            self.chan = self._open_shell()
        except Exception:
            try:
                self.client.close()
            except Exception:
                pass
            raise
        # 0.0 会让 paramiko 立刻抛 PipeTimeout → 线程 100% 空转。
        # 用 0.2s 的阻塞读更省 CPU，数据到达时条件变量会立刻唤醒。
        self.chan.settimeout(0.2)
        self.q = queue.Queue(maxsize=4000)
        self.alive = True
        self.eof = False
        self._t = threading.Thread(target=self._pump, daemon=True,
                                   name=f"term-{host_id}")
        self._t.start()

    def _open_shell(self):
        """自己开 channel：env 请求必须赶在 shell 启动之前发，paramiko 的
        invoke_shell(environment=...) 那条路径并不会真的把 env 带过去。"""
        key = f'{self.host["host"]}:{self.host.get("port")}:{self.host["username"]}'
        env = detect_locale(self.client, key)
        transport = self.client.get_transport()
        chan = transport.open_session()
        env_ok = False
        if env:
            try:
                chan.update_environment(env)
                env_ok = True
            except Exception:
                env_ok = False
        chan.get_pty(term="xterm-256color", width=self.cols, height=self.rows)
        chan.invoke_shell()
        if env and not env_ok:
            # sshd 没开 AcceptEnv 时的兜底：进去之后自己 export，再清屏盖掉
            wants = " ".join(f"{k}={v}" for k, v in env.items())
            chan.send(f"export {wants}\nclear\n")
        self.locale_env = env if env_ok else {}
        return chan

    def _pump(self):
        while self.alive:
            try:
                data = self.chan.recv(8192)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            try:
                self.q.put_nowait(data)
            except queue.Full:
                # 前端跟不上，丢掉最旧的一批，保证连接不阻塞
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.q.put_nowait(data)
                except queue.Full:
                    pass
        self.eof = True
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass

    def read_nowait(self):
        """非阻塞取一批输出。返回 (字节, 是否结束)。"""
        chunks = []
        ended = False
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                ended = True
                break
            chunks.append(item)
            if len(chunks) > 32:      # 单轮最多 256KB，别让一次循环太久
                break
        return b"".join(chunks), ended

    def has_pending(self):
        return not self.q.empty()

    def send(self, data):
        if not self.alive or self.chan is None:
            return
        if isinstance(data, str):
            data = data.encode()
        try:
            self.chan.send(data)
        except Exception:
            self.alive = False

    def resize(self, cols, rows):
        if not self.chan:
            return
        try:
            cols, rows = int(cols), int(rows)
            if cols < 10 or rows < 5:
                return
            self.cols, self.rows = cols, rows
            self.chan.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def close(self):
        self.alive = False
        try:
            if self.chan:
                self.chan.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
