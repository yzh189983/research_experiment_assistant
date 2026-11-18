"""端到端自测：走完整 API 流程，验证 方案 → 代码 → 部署 → 监控 → 总结。

前置：
    1. python app.py                                   # 服务跑在 7788
    2. python tests/fake_ssh_server.py                 # 假 SSH 在 2222

    python tests/e2e_test.py
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:7788"
S = requests.Session()
S.trust_env = False


def step(msg):
    print(f"\n── {msg}")


def main():
    step("1. 检查服务")
    r = S.get(f"{BASE}/api/experiments", timeout=5)
    r.raise_for_status()
    print("   服务正常，现有实验数:", len(r.json()))

    step("2. 添加测试服务器")
    hosts = S.get(f"{BASE}/api/hosts").json()
    host = next((h for h in hosts if h["host"] == "127.0.0.1" and h["port"] == 2222), None)
    if not host:
        r = S.post(f"{BASE}/api/hosts", json={
            "name": "本地测试机",
            "host": "127.0.0.1",
            "port": 2222,
            "username": "test",
            "auth_type": "password",
            "password": "test",
            "python_bin": sys.executable,
            "workdir": "/tmp/fake_runs",
            "gpu": 0,
        })
        r.raise_for_status()
        host_id = r.json()["id"]
        host = S.get(f"{BASE}/api/hosts").json()
        host = next(h for h in host if h["id"] == host_id)
    else:
        host_id = host["id"]
    print(f"   host #{host_id}: {host['name']} {host['username']}@{host['host']}:{host['port']}")

    step("3. 测试连接")
    r = S.post(f"{BASE}/api/hosts/{host_id}/test").json()
    if not r.get("ok"):
        print("   ✕ 连接失败:", r.get("error"))
        return 1
    print("   ✓ 连通:", r["info"].get("os", "")[:40])
    print("     python:", (r["info"].get("python") or "").strip()[:40])

    step("4. 创建实验")
    r = S.post(f"{BASE}/api/experiments", json={
        "title": "自动化流水线自测",
        "innovation": "验证一条从创新点到实验报告的自动化链路是否可用：系统应能把研究想法"
                      "拆成可检验假设，生成可无人值守运行的训练脚本，并在远程执行后自动汇总指标。",
    })
    r.raise_for_status()
    eid = r.json()["id"]
    print(f"   实验 #{eid} 已创建")

    step("5. 生成实验方案")
    S.post(f"{BASE}/api/experiments/{eid}/plan", json={}).raise_for_status()
    plan = wait_for(eid, lambda d: d.get("plan"))
    print("   ✓ 方案标题:", plan["title"])
    print("     假设数:", len(plan.get("hypothesis", [])), "| 指标:", [m["name"] for m in plan["metrics"]])

    step("6. 生成实验代码")
    S.post(f"{BASE}/api/experiments/{eid}/code", json={"host_id": host_id}).raise_for_status()
    code = wait_for(eid, lambda d: d.get("code"))
    print("   ✓ 文件:", [f["path"] for f in code["files"]])

    step("7. 启动远程实验")
    r = S.post(f"{BASE}/api/experiments/{eid}/run", json={"host_id": host_id})
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"   run #{run_id} 已排队")

    step("8. 监控直到结束")
    t0 = time.time()
    last_status = None
    while time.time() - t0 < 300:
        time.sleep(3)
        d = S.get(f"{BASE}/api/experiments/{eid}").json()
        run = d.get("latest_run") or {}
        st = run.get("status")
        if st != last_status:
            print(f"   [{int(time.time()-t0):3d}s] 状态: {st}")
            last_status = st
        if st in ("finished", "failed", "lost"):
            break
    else:
        print("   ✕ 超时未结束")
        return 1

    run = S.get(f"{BASE}/api/experiments/{eid}").json()["latest_run"]
    print(f"   结束，exit_code={run.get('exit_code')}，状态={run['status']}")

    step("9. 检查指标")
    m = S.get(f"{BASE}/api/experiments/{eid}/metrics?run_id={run_id}").json()
    series = m.get("series") or {}
    if not series:
        print("   ✕ 没有采集到指标")
        return 1
    for name, pts in series.items():
        print(f"   {name}: {len(pts)} 个点，首个 {pts[0]['v']}，末个 {pts[-1]['v']}")

    step("10. 检查自动总结")
    summ = (S.get(f"{BASE}/api/experiments/{eid}").json() or {}).get("summary")
    if not summ:
        print("   ✕ 没有生成总结")
        return 1
    print("   verdict:", summ.get("verdict"))
    print("   one_liner:", summ.get("one_liner"))
    print("   关键发现:", (summ.get("key_findings") or ["—"])[0][:90])
    print("   指标统计条数:", len(summ.get("metrics_stats", [])))

    print("\n════════ 全链路通过 ════════")
    print(f"实验详情页: {BASE}/exp/{eid}")
    return 0


def wait_for(eid, pred, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = S.get(f"{BASE}/api/experiments/{eid}").json()
        v = pred(d)
        if v:
            return v
        time.sleep(2)
    raise TimeoutError("等待超时")


if __name__ == "__main__":
    sys.exit(main())
