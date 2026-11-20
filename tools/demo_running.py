"""演示数据：造一个「运行中」的实验，用来体验监控面板与卡卡盯梢。

用法：
    .venv/bin/python tools/demo_running.py          # 创建并注入
    .venv/bin/python tools/demo_running.py --reset  # 先删旧的演示实验再重建

不会碰真实实验：演示实验标题固定以「[演示]」开头，放在个人项目里。
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core import bus, db

TITLE = "[演示] 运行中监控 · 医学图像分类"
PLAN = {
    "title": "小样本医学图像分类的对比学习",
    "core_claim": "对比学习预训练能提升小样本医学图像分类的准确率",
    "hypothesis": ["预训练 + 线性探针在小样本下优于从零训练",
                   "增广策略越强，低数据量区的收益越明显"],
    "datasets": [{"name": "CheXpert", "note": "12000 train / 2000 val"}],
    "metrics": [{"name": "accuracy", "higher_better": True, "desc": "验证集准确率"},
                {"name": "loss", "higher_better": False, "desc": "交叉熵损失"}],
    "baselines": ["从零训练的 ResNet-18"],
    "ablations": ["无预训练", "无增广"],
    "variables": {"independent": "预训练方式", "dependent": "accuracy",
                  "controlled": "seed=42 / 同网络结构"},
    "implementation": {"framework": "PyTorch", "steps": ["加载数据", "预训练", "线性探针", "评估"],
                       "estimated_runtime": "20 分钟"},
    "success_criteria": "accuracy 相对基线提升 5 个百分点以上",
    "failure_criteria": "accuracy 未超过基线或训练发散",
    "risks": ["小样本方差大，需多次 seed 复现"],
}
LOG_TEMPLATE = [
    "[env] seed=42  torch=2.3.0  cuda=True",
    "[stage] 加载数据集 CheXpert: 12000 train / 2000 val",
    "[stage] 初始化模型 resnet18 + proj_head",
    "[stage] epoch 1/20  train_loss=1.842  val_acc=0.512",
    "@@METRIC loss=1.842 step=1",
    "@@METRIC accuracy=0.512 step=1",
    "[stage] epoch 2/20  train_loss=1.531  val_acc=0.604",
    "@@METRIC loss=1.531 step=2",
    "@@METRIC accuracy=0.604 step=2",
    "[stage] epoch 3/20  train_loss=1.318  val_acc=0.671",
    "@@METRIC loss=1.318 step=3",
    "@@METRIC accuracy=0.671 step=3",
    "[stage] epoch 4/20  train_loss=1.205  val_acc=0.703",
    "@@METRIC loss=1.205 step=4",
    "@@METRIC accuracy=0.703 step=4",
    "[stage] epoch 5/20  train_loss=1.122  val_acc=0.726",
    "@@METRIC loss=1.122 step=5",
    "@@METRIC accuracy=0.726 step=5",
    "[stage] epoch 6/20  train_loss=1.064  val_acc=0.741",
    "@@METRIC loss=1.064 step=6",
    "@@METRIC accuracy=0.741 step=6",
    "[warn] val 图像 000731 解码缓慢，已用缓存继续",
    "[stage] epoch 7/20  train_loss=1.021  val_acc=0.752",
    "@@METRIC loss=1.021 step=7",
    "@@METRIC accuracy=0.752 step=7",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--uid", type=int, default=2)
    args = ap.parse_args()

    with config.as_user(args.uid):
        personal = db.ensure_personal_project(args.uid)
        old = [e for e in db.list_experiments(uid=args.uid)
               if (e.get("title") or "").startswith("[演示]")]
        if args.reset:
            for e in old:
                for r in db.list_runs(e["id"], uid=args.uid):
                    db.ex("DELETE FROM metrics WHERE run_id=?", (r["id"],))
                    db.ex("DELETE FROM stream_events WHERE run_id=?", (r["id"],))
                db.ex("DELETE FROM runs WHERE experiment_id=?", (e["id"],))
                db.ex("DELETE FROM experiments WHERE id=?", (e["id"],))
            old = []

        if old:
            eid = old[0]["id"]
            print(f"复用已有演示实验 #{eid}")
        else:
            eid = db.create_experiment(TITLE, "对比学习在小样本医学图像上的效果", uid=args.uid,
                                       project_id=personal)
            print(f"创建演示实验 #{eid}")

        db.update_experiment(eid, status="running", plan_json=json.dumps(PLAN, ensure_ascii=False))
        started = time.time() - 8 * 60  # 假装已跑 8 分钟
        rid = db.ex(
            "INSERT INTO runs(user_id,experiment_id,host_id,remote_dir,pid,status,started_at,"
            "heartbeat_at,log_tail,metrics_json,env_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (args.uid, eid, 1, "~/demo_run", "38271", "running", started, time.time(),
             "\n".join(LOG_TEMPLATE), "{}",
             json.dumps({"seed": 42, "python": "3.10", "gpu": "A100"}, ensure_ascii=False)))

        for i in range(1, 8):
            db.add_metric(rid, "accuracy", round(0.50 + 0.04 * i + 0.01 * math.sin(i), 4), i)
            db.add_metric(rid, "loss", round(1.95 - 0.13 * i, 4), i)

        # 落库事件，SSE 断线补发时能拿到
        for line in LOG_TEMPLATE:
            kind = "metric" if line.startswith("@@METRIC") else \
                   "stage" if "[stage]" in line else "warn" if "[warn]" in line else "log"
            db.add_stream_event(eid, "log", {"line": line}, run_id=rid, uid=args.uid)
            db.add_event(eid, rid, line, kind=kind, uid=args.uid)
        db.add_stream_event(eid, "status", {"status": "running", "run_id": rid,
                                            "experiment_status": "running"}, run_id=rid, uid=args.uid)
        db.add_stream_event(eid, "gpu", {"gpus": [
            {"index": 0, "name": "NVIDIA A100", "util": 87,
             "mem_used": 28672, "mem_total": 40960}]}, run_id=rid, uid=args.uid)

        # 推给当前在线的浏览器（内存订阅者）
        bus.publish(eid, {"type": "status", "status": "running", "run_id": rid,
                          "experiment_status": "running"})
        bus.publish(eid, {"type": "gpu", "gpus": [
            {"index": 0, "name": "NVIDIA A100", "util": 87,
             "mem_used": 28672, "mem_total": 40960}]})
        for line in LOG_TEMPLATE[-8:]:
            bus.publish(eid, {"type": "log", "line": line})
        for i in range(1, 8):
            bus.publish(eid, {"type": "metric", "name": "accuracy",
                              "value": round(0.50 + 0.04 * i + 0.01 * math.sin(i), 4), "step": i})
            bus.publish(eid, {"type": "metric", "name": "loss",
                              "value": round(1.95 - 0.13 * i, 4), "step": i})

        print(f"演示数据已注入：实验 #{eid}，run #{rid}")
        print(f"打开：https://127.0.0.1:7788/exp/{eid}")
        print("注意：这是模拟数据，点「停止」不会影响真实进程；用 --reset 可重建。")


if __name__ == "__main__":
    main()
