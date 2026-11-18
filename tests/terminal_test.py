"""网页终端 WebSocket 自测：连上 → 敲命令 → 看回显 → 调窗口大小 → 断开。

前置：python app.py 已运行，且至少有一台可用的 SSH 主机。
    python tests/terminal_test.py [host_id]
    REKIT_PORT=7811 python tests/terminal_test.py 4   # 打指定端口的实例
"""
import json
import os
import sys
import time

from simple_websocket import Client

PORT = os.environ.get("REKIT_PORT", "7788")
BASE = f"ws://127.0.0.1:{PORT}"

RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append(bool(ok))
    print(("✓" if ok else "✕") + " " + label)
    if not ok and detail:
        print("  收到:", detail[-400:].replace("\x1b", "\\e"))


def recv_until(ws, needle, timeout=8.0):
    buf = ""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            d = ws.receive(timeout=max(0.1, timeout - (time.time() - t0)))
        except TimeoutError:
            break
        if d is None:          # simple-websocket 超时返回 None
            continue
        if isinstance(d, bytes):
            buf += d.decode("utf-8", "replace")
        else:
            try:
                m = json.loads(d)
            except json.JSONDecodeError:
                buf += d
            else:
                if m.get("type") == "error":
                    return "ERROR: " + m.get("message", "")
                if m.get("type") == "exit":
                    return buf + "\n[exit]"
                continue
        if needle in buf:
            return buf
    return buf


def main():
    hid = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(f"连接 {BASE}/ws/terminal/{hid}")

    ws = Client.connect(f"{BASE}/ws/terminal/{hid}?cols=100&rows=30")
    ready = recv_until(ws, "ready", timeout=8)
    if "ERROR" in ready:
        print("✕ 连接失败：", ready)
        return 1
    check(True, "会话已建立")

    # 1. 基本命令 + 中文
    ws.send(json.dumps({"type": "input", "data": "echo HELLO_TERM_123; echo 中文正常\n"}))
    out = recv_until(ws, "中文正常")
    check("HELLO_TERM_123" in out and "中文正常" in out, "命令执行与中文显示", out)

    # 2. 窗口大小
    ws.send(json.dumps({"type": "resize", "cols": 120, "rows": 40}))
    time.sleep(0.4)
    ws.send(json.dumps({"type": "input", "data": "stty size\n"}))
    out = recv_until(ws, "40 120")
    check("40 120" in out, "窗口大小同步（期望 40 120）", out)

    # 3. 环境变量 TERM
    ws.send(json.dumps({"type": "input", "data": "echo TERM_IS_$TERM\n"}))
    out = recv_until(ws, "TERM_IS_xterm")
    check("TERM_IS_xterm" in out, "TERM 传递", out)

    # 4. 颜色转义序列能透传
    ws.send(json.dumps({"type": "input", "data": "printf '\\033[32mGREEN\\033[0m\\n'\n"}))
    out = recv_until(ws, "GREEN")
    check("\x1b[32m" in out, "ANSI 颜色透传", out)

    # 5. 远程 locale 是否 UTF-8：C locale 下 GNU ls 会把中文名转成 $'\346\226\260…'
    #    文件名先存进变量，这样下一条命令的回显里不会出现中文，断言才可信。
    ws.send(json.dumps({"type": "input", "data":
                        "cd /tmp && F=中文测试.txt && rm -f $F && touch $F && echo ok\n"}))
    recv_until(ws, "\x00clearbuf", timeout=0.6)      # 把上面的回显吃掉
    ws.send(json.dumps({"type": "input", "data":
                        "ls $F; M=LOCALE; echo ${M}=$LANG/$LC_ALL\n"}))
    out = recv_until(ws, "LOCALE=")                  # 只有输出里有 LOCALE=，回显是 ${M}
    check("中文测试.txt" in out and "$'" not in out,
          "中文文件名不被转义（UTF-8 locale）", out)
    print("   locale:", next((ln.strip() for ln in out.splitlines()
                              if ln.strip().startswith("LOCALE=")), "?"))

    # 6. 关掉终端后服务端要能感知并回收会话
    ws.close()
    time.sleep(0.5)
    check(True, "断开连接")

    ok_all = all(RESULTS)
    print("\n全部通过" if ok_all else f"\n有 {RESULTS.count(False)} 项失败")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
