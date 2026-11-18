"""本地假 SSH 服务器 —— 仅用于端到端自测，不参与业务逻辑。

它在本机 2222 端口提供一个真实 SSH 协议端点：
- 密码认证 test/test
- exec 请求直接在本机 bash 执行（因此训练脚本是真跑的）
- sftp 子系统映射到本机文件系统

用途：没有可用远程服务器时，验证 上传 → 建环境 → 启动 → 监控 → 总结 全链路。

    python tests/fake_ssh_server.py
"""
import errno as _errno
import os
import posixpath
import socket
import subprocess
import threading

import paramiko
from paramiko import (AUTH_FAILED, AUTH_SUCCESSFUL, SFTP_FAILURE, SFTP_NO_SUCH_FILE,
                      SFTP_PERMISSION_DENIED, SFTPAttributes, SFTPHandle,
                      SFTPServerInterface, ServerInterface)
from paramiko.common import OPEN_SUCCEEDED

HOST, PORT = "127.0.0.1", 2222
USER, PASS = "test", "test"

# 内存里生成一次，避免落盘私钥触发系统权限拦截
_HOST_KEY = paramiko.RSAKey.generate(2048)


def _errno_of(e):
    if isinstance(e, FileNotFoundError):
        return SFTP_NO_SUCH_FILE
    if isinstance(e, PermissionError):
        return SFTP_PERMISSION_DENIED
    if getattr(e, "errno", None) == _errno.ENOENT:
        return SFTP_NO_SUCH_FILE
    return SFTP_FAILURE


class LocalSFTPServer(SFTPServerInterface):
    """把 SFTP 请求映射到本机文件系统。"""

    def _p(self, path):
        return posixpath.normpath(posixpath.join("/", path or "/"))

    def list_folder(self, path):
        p = self._p(path)
        out = []
        try:
            for name in os.listdir(p):
                a = SFTPAttributes.from_stat(os.lstat(os.path.join(p, name)))
                a.filename = name
                out.append(a)
        except OSError as e:
            return _errno_of(e)
        return out

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._p(path)))
        except OSError as e:
            return _errno_of(e)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._p(path)))
        except OSError as e:
            return _errno_of(e)

    def open(self, path, flags, attr):
        p = self._p(path)
        write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        # 必须二进制模式：paramiko 的 SFTPHandle 按字节偏移 seek/write
        mode = ("ab" if flags & os.O_APPEND else "wb") if write else "rb"
        try:
            f = open(p, mode)
        except OSError as e:
            return _errno_of(e)
        h = SFTPHandle(flags)
        h.filename = p
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._p(path))
            return 0
        except OSError as e:
            return _errno_of(e)

    def rename(self, old, new):
        try:
            os.rename(self._p(old), self._p(new))
            return 0
        except OSError as e:
            return _errno_of(e)

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._p(path))
            return 0
        except OSError as e:
            return _errno_of(e)

    def rmdir(self, path):
        try:
            os.rmdir(self._p(path))
            return 0
        except OSError as e:
            return _errno_of(e)

    def chattr(self, path, attr):
        return 0

    def symlink(self, target, path):
        return SFTP_FAILURE

    def readlink(self, path):
        return SFTP_FAILURE

    def canonicalize(self, path):
        return self._p(path)


class Server(ServerInterface):
    def __init__(self):
        self._env = {}          # 客户端通过 SSH env 请求传来的变量
        self._pty = {"term": "xterm-256color", "width": 80, "height": 24}
        self._fd = None

    def check_auth_password(self, username, password):
        return AUTH_SUCCESSFUL if (username == USER and password == PASS) else AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED

    def check_channel_env_request(self, channel, name, value):
        """记录客户端通过 SSH 传来的环境变量（真 sshd 在 AcceptEnv 允许时也这样）。"""
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        self._env[name] = value
        return True

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        if isinstance(term, bytes):
            term = term.decode("utf-8", "replace")
        self._pty = {"term": term or "xterm-256color",
                     "width": width or 80, "height": height or 24}
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self._shell, args=(channel,), daemon=True).start()
        return True

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        """前端拖窗口时把新的行列号写回伪终端，stty size 才能跟着变。"""
        import fcntl
        import struct
        import termios
        self._pty["width"], self._pty["height"] = width or 80, height or 24
        fd = getattr(self, "_fd", None)
        if fd is not None:
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", self._pty["height"], self._pty["width"], 0, 0))
            except Exception:
                pass
        return True

    def _shell(self, channel):
        """把本地 bash 挂到伪终端上，双向转发——这样网页终端能真的交互。"""
        import fcntl
        import pty
        import select
        import struct
        import termios

        dims = getattr(self, "_pty", {"term": "xterm-256color", "width": 80, "height": 24})
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(getattr(self, "_env", {}) or {})
            term = dims.get("term") or "xterm-256color"
            if isinstance(term, bytes):
                term = term.decode("utf-8", "replace")
            os.environ["TERM"] = str(term)
            os.environ["PS1"] = "\\u@fake-sshd:\\w$ "
            os.chdir("/tmp")
            try:
                os.execv("/bin/bash", ["bash", "--norc", "--noprofile", "-i"])
            except Exception:
                os._exit(1)

        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", dims["height"], dims["width"], 0, 0))
        except Exception:
            pass

        self._fd = fd      # 供 window-change 请求改窗口大小

        stop = threading.Event()

        def pump_out():
            while not stop.is_set():
                try:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if not r:
                        continue
                    data = os.read(fd, 8192)
                    if not data:
                        break
                    channel.send(data)
                except OSError:
                    break
            stop.set()
            try:
                channel.close()
            except Exception:
                pass

        threading.Thread(target=pump_out, daemon=True).start()

        try:
            while not stop.is_set():
                data = channel.recv(8192)
                if not data:
                    break
                os.write(fd, data)
        except Exception:
            pass

        stop.set()
        self._fd = None
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass

    def check_channel_exec_request(self, channel, command):
        cmd = command.decode("utf-8", "replace") if isinstance(command, bytes) else command
        threading.Thread(target=self._run, args=(channel, cmd), daemon=True).start()
        return True

    @staticmethod
    def _run(channel, cmd):
        env = dict(os.environ)
        env["PATH"] = env.get("PATH", "") + ":/usr/local/bin:/opt/homebrew/bin"
        try:
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, cwd="/tmp", env=env)
            for line in iter(p.stdout.readline, b""):
                channel.send(line)
            p.wait()
            channel.send_exit_status(p.returncode)
        except Exception as e:
            try:
                channel.send(f"[fake-sshd] error: {e}\n".encode())
                channel.send_exit_status(1)
            except Exception:
                pass
        finally:
            try:
                channel.close()
            except Exception:
                pass


def handle(client_sock):
    transport = None
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(_HOST_KEY)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, LocalSFTPServer)
        transport.start_server(server=Server())
    except Exception as e:
        print(f"[fake-sshd] handshake error: {type(e).__name__}: {e}", flush=True)
        if transport:
            transport.close()
        return

    # 保持 transport 存活直到对端断开
    while transport.is_active():
        threading.Event().wait(1)
    transport.close()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, PORT))
    sock.listen(20)
    print(f"[fake-sshd] listening on {HOST}:{PORT}  user={USER} pass={PASS}", flush=True)
    while True:
        try:
            client, _ = sock.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
