"""实验监控循环：盯进程、盯日志、盯 GPU，结束就自动总结。

线程模型：每个 run 一个 daemon 线程，由 deploy_and_launch 在部署成功后拉起。
"""
import collections
import posixpath
import threading
import time

import config
from core import bus, db, ssh_runner

MAX_RUNTIME_SEC = 24 * 3600        # 最长盯 24 小时
DEAD_CONFIRM_TICKS = 3             # 连续 3 次确认进程死了才算失败
CONN_FAIL_TICKS = 5                # 连续 5 次连不上判定失联

_running = {}          # run_id -> thread
_lock = threading.Lock()


def is_watching(run_id):
    t = _running.get(run_id)
    return bool(t and t.is_alive())


def watch(run_id, exp_id, host_id, remote_dir, interval=None, uid=None):
    if interval is None:
        interval = config.load_config()["monitor"]["interval_sec"]
    # 监控在独立线程里跑，ContextVar 不会跨线程继承，显式带上 uid
    if uid is None:
        uid = config.current_uid()
    with _lock:
        if run_id in _running and _running[run_id].is_alive():
            return _running[run_id]
        th = threading.Thread(
            target=_loop, args=(run_id, exp_id, host_id, remote_dir, interval, uid),
            daemon=True, name=f"monitor-{run_id}")
        _running[run_id] = th
        th.start()
        return th


def _loop(run_id, exp_id, host_id, remote_dir, interval, uid=None):
    with config.as_user(uid):
        _loop_inner(run_id, exp_id, host_id, remote_dir, interval)


def _loop_inner(run_id, exp_id, host_id, remote_dir, interval):
    host = db.get_host(host_id, include_secrets=True)
    cfg = config.load_config()["monitor"]
    t0 = time.time()
    offset = 0
    dead_ticks = 0
    fail_ticks = 0
    recent = collections.deque(maxlen=300)
    gpu_every = max(1, int(10 / max(interval, 1)))   # 约每 10 秒采一次 GPU
    tick = 0

    def sink(line):
        recent.append(line)

    while True:
        time.sleep(interval)
        tick += 1
        # 心跳：让重启后的 bootstrap 能区分"真在跑"和"线程已死但状态还是 running"
        try:
            db.touch_run(run_id)
        except Exception:
            pass
        if time.time() - t0 > MAX_RUNTIME_SEC:
            _finish(run_id, exp_id, "failed", "超过最长监控时长，已停止监控（远程进程可能仍在跑）")
            return

        client = None
        try:
            client = ssh_runner.get_cached_client(host, timeout=15)
            fail_ticks = 0
            sftp = client.open_sftp()

            # 1) 增量日志
            offset = ssh_runner._stream_log(sftp, remote_dir, "run.log", offset, exp_id, run_id, sink)
            if recent:
                ssh_runner.flush_tail(run_id, list(recent))

            # 2) 退出码 / 进程存活
            exited, code = _read_exit(sftp, remote_dir)
            alive = _is_alive(sftp, remote_dir)

            # 3) GPU 快照
            if host.get("gpu") and tick % gpu_every == 0:
                try:
                    _, out, _ = ssh_runner.run(
                        client, "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total "
                                "--format=csv,noheader,nounits 2>/dev/null || true", timeout=15)
                    gpus = ssh_runner.parse_gpu(out)
                    if gpus:
                        bus.gpu(exp_id, gpus, run_id)
                except Exception:
                    pass

            sftp.close()
            # client 由缓存池管理，不在这里关闭；异常分支才 drop
            client = None

            if exited:
                # 把最后一点日志读完
                _drain(host, remote_dir, offset, exp_id, run_id, sink, recent)
                status = "finished" if code == "0" else "failed"
                _finish(run_id, exp_id, status, f"进程退出，exit_code={code}", exit_code=code)
                return

            if not alive:
                dead_ticks += 1
                if dead_ticks >= DEAD_CONFIRM_TICKS:
                    _drain(host, remote_dir, offset, exp_id, run_id, sink, recent)
                    _finish(run_id, exp_id, "failed", "进程已消失且没有写入退出码（可能被 OOM 杀掉或异常中断）")
                    return
            else:
                dead_ticks = 0

            # 卡住提醒（日志长时间不增长）
            if tick % 40 == 0:
                _check_stall(run_id, exp_id, offset, cfg)

        except Exception as e:
            import traceback
            traceback.print_exc()
            fail_ticks += 1
            if client:
                try:
                    ssh_runner.drop_cached_client(host)
                except Exception:
                    pass
            if fail_ticks == 3:
                bus.note(exp_id, f"监控连接异常：{e}，正在重试…", "warn", run_id)
            if fail_ticks >= CONN_FAIL_TICKS:
                _finish(run_id, exp_id, "lost", f"连续 {fail_ticks} 次无法连接服务器，监控中止：{e}")
                return


def _read_exit(sftp, remote_dir):
    try:
        with sftp.open(posixpath.join(remote_dir, ".run.exit"), "r") as f:
            v = f.read().decode("utf-8", "replace").strip()
        return True, v or "0"
    except IOError:
        return False, ""


def _is_alive(sftp, remote_dir):
    try:
        with sftp.open(posixpath.join(remote_dir, ".run.pid"), "r") as f:
            pid = f.read().decode("utf-8", "replace").strip()
        return bool(pid)
    except IOError:
        return False


_offset_cache = {}


def _check_stall(run_id, exp_id, offset, cfg):
    key = run_id
    prev = _offset_cache.get(key)
    if prev is not None and offset <= prev[0] and time.time() - prev[1] > cfg["stall_warn_sec"]:
        bus.note(exp_id, f"日志已 {int(time.time()-prev[1])} 秒没有增长，实验可能卡住了", "warn", run_id)
        _offset_cache[key] = (offset, time.time())
    elif prev is None or offset > prev[0]:
        _offset_cache[key] = (offset, time.time())


def _drain(host, remote_dir, offset, exp_id, run_id, sink, recent):
    try:
        client = ssh_runner.get_cached_client(host, timeout=15)
        sftp = client.open_sftp()
        for _ in range(3):
            offset = ssh_runner._stream_log(sftp, remote_dir, "run.log", offset, exp_id, run_id, sink)
        sftp.close()
    except Exception:
        ssh_runner.drop_cached_client(host)
    if recent:
        ssh_runner.flush_tail(run_id, list(recent))


def _finish(run_id, exp_id, status, message, exit_code=None):
    fields = {"status": status, "finished_at": db.now()}
    if exit_code is not None:
        fields["exit_code"] = str(exit_code)
    db.update_run(run_id, **fields)
    level = "info" if status == "finished" else "error"
    bus.note(exp_id, message, level, run_id)
    db.add_event(exp_id, run_id, message, kind="stage", level=level)

    exp_status = "done" if status == "finished" else "failed"
    db.update_experiment(exp_id, status=exp_status)
    bus.status(exp_id, exp_status, run_id, {"run_status": status})

    # 卡卡播报：运行完成 / 失败（项目群聊，继承项目角色）
    try:
        from core import kaka
        kaka._broadcast_for_exp(
            exp_id, "run_finished" if status == "finished" else "run_failed",
            f"run #{run_id} {status}", uid=config.current_uid())
    except Exception:
        pass

    # run 结束：释放自动占用的 GPU 排程，空闲时在项目群提醒
    try:
        released = db.release_gpu_reservation_for_run(run_id)
        if released:
            kaka.notify_idle_gpu(released["project_id"], uid=config.current_uid())
    except Exception:
        pass

    # 失败自动诊断（规则签名，立即可用）
    if status != "finished":
        try:
            from core import diagnose
            dg = diagnose.diagnose_run(run_id)
            if dg:
                import json as _json
                db.update_run(run_id, diagnosis_json=_json.dumps(dg, ensure_ascii=False))
                db.add_event(exp_id, run_id,
                             f"失败诊断：{dg['cause']} — {', '.join(dg['fixes'][:2])}",
                             kind="stage", level="error")
                bus.note(exp_id, f"诊断：{dg['cause']}", "error", run_id)
        except Exception:
            pass

    # Webhook 通知（完成 / 失败）
    try:
        from core import notify
        notify_event = "finished" if status == "finished" else "failed"
        notify.send(notify_event, exp_id, message=message,
                    url_path=f"/exp/{exp_id}")
    except Exception:
        pass

    # 自动总结
    from core import summarizer
    try:
        summary = summarizer.summarize(run_id, exp_id, status=status)
        try:
            from core import kaka
            verdict = (summary or {}).get("verdict") or (summary or {}).get("one_liner") or "结果已生成"
            kaka._broadcast_for_exp(exp_id, "summary_ready", verdict[:180],
                                    uid=config.current_uid())
        except Exception:
            pass
    except Exception as e:
        msg = f"结果总结失败：{e}"
        bus.note(exp_id, msg, "error", run_id)
        db.add_event(exp_id, run_id, msg, kind="error", level="error")
        bus.summary(exp_id, {"error": str(e)}, run_id)
