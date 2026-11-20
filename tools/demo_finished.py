"""演示数据：造一个「已跑完、待总结」的实验，用来体验「实验后：总结实验结果」。

打开实验页后，卡卡工作台会显示「让卡卡总结这次结果」，点它即可触发总结：
  POST /api/runs/<rid>/summarize  ->  summarizer.summarize(force=True)
  uid=2 配了 DeepSeek，会走真实 LLM 生成语义结论；没配则规则降级，也能看结构。

关键：run 状态 finished 且 summary_json 为空，卡卡才会给出「让卡卡总结」动作。
不会碰真实实验：演示实验标题固定以「[演示]」开头，放在个人项目里。

用法：
    python3 tools/demo_finished.py          # 创建并注入（复用旧的演示实验）
    python3 tools/demo_finished.py --reset  # 先删旧的「[演示] 已跑完」实验再重建
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core import bus, db, workspace

TITLE = "[演示] 已跑完 · 医学图像分类（待总结）"
INNOVATION = ("用自监督对比学习在有限标注的胸部 X 光片上预训练，"
              "再以线性探针评估特征质量，检验其是否优于从零训练。")
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
# 预注册判据：与指标名对应，locked 后核验面板会给出「通过/未通过」
CRITERIA = {
    "success": [{"metric": "accuracy", "op": "gte", "threshold": 0.85}],
    "failure": [{"metric": "loss", "op": "gt", "threshold": 1.0}],
}
# 一份脚本，让卡卡工作台不显示「写实验脚本」阻塞，聚焦在「总结结果」
CODE = {
    "entrypoint": "main.py", "framework": "PyTorch",
    "files": [{"path": "main.py", "content": (
        "# 演示脚本：对比学习预训练 + 线性探针\n"
        "import torch\n\n"
        "def report(name, value, step):\n"
        "    print(f'@@METRIC {name}={value:.4f} step={step}', flush=True)\n\n"
        "def main():\n"
        "    torch.manual_seed(42)\n"
        "    best = 0.8956\n"
        "    print(f'[result] val_accuracy = {best:.4f}', flush=True)\n"
        "    report('accuracy', best, 18)\n"
        "    report('loss', 0.3128, 18)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n")}],
}

# 20 轮完整日志 + 程序自己打印的结果汇总（总结器会重点读日志尾部）
LOG_TEMPLATE = [
    "[env] seed=42  torch=2.3.0  cuda=True",
    "[stage] 加载数据集 CheXpert: 12000 train / 2000 val",
    "[stage] 初始化模型 resnet18 + proj_head（SimCLR 预训练 100 epoch）",
]
for _i in range(1, 21):
    _train_loss = round(1.84 - 0.075 * _i + 0.004 * (_i % 3), 4)
    _val_acc = round(0.512 + 0.019 * _i + 0.004 * (_i % 4), 4)
    LOG_TEMPLATE += [
        f"[stage] epoch {_i}/20  train_loss={_train_loss:.4f}  val_acc={_val_acc:.4f}",
        f"@@METRIC loss={_train_loss:.4f} step={_i}",
        f"@@METRIC accuracy={_val_acc:.4f} step={_i}",
    ]
LOG_TEMPLATE += [
    "[stage] 线性探针评估完成，加载最佳 checkpoint",
    "[result] ===== 最终结果汇总 =====",
    "[result] val_accuracy = 0.8956 (best@step18), val_loss = 0.3128",
    "[result] baseline(从零训练 ResNet-18) accuracy = 0.8012",
    "[result] 消融: 无预训练 accuracy = 0.7284; 无增广 accuracy = 0.8531",
    "[result] 相对基线提升 +9.44 个百分点，超过预设的 +5pp 判据",
    "[done] 训练正常结束，exit_code=0，耗时 19 分 42 秒",
]

# 与日志一致的指标序列（accuracy 单调向上并收敛，loss 单调向下）
ACC = [round(0.512 + 0.019 * i + 0.004 * (i % 4), 4) for i in range(1, 21)]
LOSS = [round(1.84 - 0.075 * i + 0.004 * (i % 3), 4) for i in range(1, 21)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--uid", type=int, default=2)
    args = ap.parse_args()

    with config.as_user(args.uid):
        personal = db.ensure_personal_project(args.uid)
        old = [e for e in db.list_experiments(uid=args.uid)
               if (e.get("title") or "").startswith(TITLE)]
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
            # 清理该实验旧 run，避免 latest_run 指向旧的
            for r in db.list_runs(eid, uid=args.uid):
                db.ex("DELETE FROM metrics WHERE run_id=?", (r["id"],))
                db.ex("DELETE FROM stream_events WHERE run_id=?", (r["id"],))
            db.ex("DELETE FROM runs WHERE experiment_id=?", (eid,))
            print(f"复用已有演示实验 #{eid}")
        else:
            eid = db.create_experiment(TITLE, INNOVATION, uid=args.uid, project_id=personal)
            print(f"创建演示实验 #{eid}")

        now = time.time()
        db.update_experiment(
            eid, status="finished", plan_json=json.dumps(PLAN, ensure_ascii=False),
            code_json=json.dumps(CODE, ensure_ascii=False),
            criteria_json=json.dumps(CRITERIA, ensure_ascii=False),
            criteria_locked=1, criteria_locked_at=now - 30 * 60)
        workspace.write_files(eid, CODE)
        started = now - 20 * 60 - 42  # 假装跑了 19 分 42 秒
        finished = now - 20 * 60
        rid = db.ex(
            "INSERT INTO runs(user_id,experiment_id,host_id,remote_dir,pid,status,exit_code,"
            "started_at,finished_at,heartbeat_at,log_tail,metrics_json,env_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (args.uid, eid, 1, "~/demo_finished", "39401", "finished", "0",
             started, finished, finished, "\n".join(LOG_TEMPLATE), "{}",
             json.dumps({"seed": 42, "python": "3.10", "gpu": "A100"}, ensure_ascii=False)))

        for i in range(1, 21):
            db.add_metric(rid, "accuracy", ACC[i - 1], i)
            db.add_metric(rid, "loss", LOSS[i - 1], i)

        # 落库事件：SSE 断线补发 / 页面刷新后可回放
        for line in LOG_TEMPLATE:
            kind = ("metric" if line.startswith("@@METRIC") else
                    "summary" if "[result]" in line else
                    "stage" if "[stage]" in line else
                    "log" if "[env]" in line else "log")
            db.add_stream_event(eid, "log", {"line": line}, run_id=rid, uid=args.uid)
            db.add_event(eid, rid, line, kind=kind, uid=args.uid)
        db.add_stream_event(eid, "status", {"status": "finished", "run_id": rid,
                                            "experiment_status": "finished"},
                            run_id=rid, uid=args.uid)
        db.add_audit(eid, "run", "演示运行完成",
                     {"exit_code": 0, "duration": "19分42秒", "val_accuracy": 0.8956},
                     decision="finished", run_id=rid, uid=args.uid)

        # 推给在线浏览器（若页面已打开会立刻刷新）
        bus.publish(eid, {"type": "status", "status": "finished", "run_id": rid,
                          "experiment_status": "finished"})
        for line in LOG_TEMPLATE[-8:]:
            bus.publish(eid, {"type": "log", "line": line})

        print(f"演示数据已注入：实验 #{eid}，run #{rid}（finished，暂无结论）")
        print(f"打开：https://127.0.0.1:7788/exp/{eid}")
        print("触发方式：卡卡工作台点「让卡卡总结这次结果」→ 生成实验后结论。")
        print("注意：这是模拟数据，summary_json 故意留空以便你手动触发；--reset 可重建。")


if __name__ == "__main__":
    main()
