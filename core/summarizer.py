"""实验结果自动总结：指标聚合 + 日志分析 → 结构化结论报告。"""
import json
import math
import time

from core import db, llm, verify

SYSTEM = """你是严谨的科研合作者，负责审阅一次实验的原始产出并给出结论。
你只依据提供的数据说话，不做无根据的推断。指标没有收敛、样本太小、只跑了单次，
都要在局限性里明确说出来。输出合法 JSON。"""

PROMPT = """一次实验刚刚跑完，请审阅结果并给出结论。

<研究者的创新点>
{innovation}
</研究者的创新点>

<原定实验方案>
{plan}
</原定实验方案>

<运行状态>
状态：{status}
退出码：{exit_code}
耗时：{duration}
</运行状态>

<指标统计（数值区间为全程序列）>
{metrics}
</指标统计>

<日志尾部（含程序自己打印的结果汇总）>
{log_tail}
</日志尾部>

请按下面 JSON 结构输出结论：

{{
  "verdict": "支持假设 / 部分支持 / 不支持 / 证据不足（四选一）",
  "one_liner": "一句话结论，不超过 40 字，要有数字",
  "hypothesis_results": [
    {{"hypothesis": "原假设原文", "verdict": "supported/refuted/inconclusive", "evidence": "支撑或反驳它的具体数据"}}
  ],
  "key_findings": ["3-5 条关键发现，每条必须带具体数值"],
  "comparison": "实验组 vs 基线/消融的对比结论，有数字就写数字；没跑出对比就直说",
  "anomalies": ["运行中的异常：崩溃、nan、指标不收敛、数据降级等；没有就给空数组"],
  "limitations": ["这次实验结论的局限：规模、单次运行、未收敛、数据来源等"],
  "next_steps": ["下一步最该做的 2-4 件事，要具体可执行"],
  "paper_paragraph": "可以直接放进论文 Results 段的中文段落，150-250 字，客观陈述，包含关键数值"
}}

注意：
- 如果运行失败（status 不是 finished），verdict 用「证据不足」，并把失败原因写进 anomalies。
- 不要编造数据里没有的数字。日志里明确写了的结果可以引用。
- 如果指标全程单调下降且未收敛，要在 limitations 里指出「训练未收敛」。
只输出 JSON。"""


def _stats(run_id, plan):
    series = db.metrics_series(run_id, limit=100000)
    higher = {m["name"]: m.get("higher_better", True) for m in plan.get("metrics", [])}
    out = []
    for name, pts in series.items():
        vals = [p["v"] for p in pts if p["v"] is not None and not _isnan(p["v"])]
        if not vals:
            continue
        hb = higher.get(name, True)
        out.append({
            "name": name,
            "points": len(vals),
            "first": round(vals[0], 6),
            "last": round(vals[-1], 6),
            "best": round(max(vals) if hb else min(vals), 6),
            "last_step": pts[-1].get("s"),
            "min": round(min(vals), 6),
            "max": round(max(vals), 6),
            "higher_better": hb,
            "change_pct": (round((vals[-1] - vals[0]) / abs(vals[0]) * 100, 2)
                           if abs(vals[0]) > 1e-9 else None),
        })
    out.sort(key=lambda x: -x["points"])
    return out


def _isnan(v):
    try:
        return math.isnan(float(v)) or math.isinf(float(v))
    except (TypeError, ValueError):
        return True


def summarize(run_id, exp_id, status=None, force=False):
    run = db.get_run(run_id)
    if not run:
        raise ValueError("run 不存在")
    if run.get("summary_json") and not force:
        return json.loads(run["summary_json"])

    exp = db.get_experiment(exp_id)
    plan = json.loads(exp.get("plan_json") or "{}")
    status = status or run.get("status") or "finished"
    duration = ""
    if run.get("started_at"):
        end = run.get("finished_at") or time.time()
        sec = max(0, end - run["started_at"])
        duration = f"{int(sec // 60)} 分 {int(sec % 60)} 秒"
    metrics = _stats(run_id, plan)

    summary = None
    if llm.is_configured():
        try:
            summary = llm.chat_json(
                [{"role": "user", "content": PROMPT.format(
                    innovation=exp.get("innovation", ""),
                    plan=json.dumps(plan, ensure_ascii=False, indent=2),
                    status=status,
                    exit_code=run.get("exit_code") or "",
                    duration=duration,
                    metrics=json.dumps(metrics, ensure_ascii=False, indent=2),
                    log_tail=(run.get("log_tail") or "(无日志)")[-6000:],
                )}],
                system=SYSTEM, temperature=0.25)
        except Exception as e:
            summary = None
            err = str(e)
        else:
            err = None
            usage = llm.last_usage()
            if usage:
                db.add_usage("summary", usage.get("model", ""),
                             usage.get("prompt_tokens", 0),
                             usage.get("completion_tokens", 0),
                             request_id=usage.get("request_id", ""),
                             experiment_id=exp_id, run_id=run_id)
    else:
        err = "LLM 未配置"

    if summary is None:
        summary = _rule_based(metrics, status, err)

    summary["generated_at"] = time.time()
    summary["metrics_stats"] = metrics
    summary["duration"] = duration
    summary["run_status"] = status
    summary["verification"] = verify.verify_summary(
        summary, metrics, run.get("log_tail") or "",
        criteria=exp.get("criteria_json"),
        criteria_locked=bool(exp.get("criteria_locked")),
        plan=plan)
    summary["needs_review"] = bool(summary["verification"]["unverified_numbers"])
    if exp.get("mode") == "copilot":
        db.update_experiment(exp_id, checkpoint="done")

    db.update_run(run_id, summary_json=json.dumps(summary, ensure_ascii=False))
    db.add_audit(exp_id, "summary", "生成实验总结",
                 {"verdict": summary.get("verdict"),
                  "unverified_numbers": len(summary["verification"]["unverified_numbers"]),
                  "criteria_status": summary["verification"]["criteria"]["summary"]},
                 decision=summary.get("verdict", ""), run_id=run_id)
    db.add_event(exp_id, run_id,
                 f"实验总结已生成：{summary.get('verdict','')} — {summary.get('one_liner','')}",
                 kind="summary", level="info")
    try:
        from core import bus
        bus.summary(exp_id, summary, run_id)
    except Exception:
        pass
    return summary


def _rule_based(metrics, status, err):
    """LLM 不可用时的规则版总结：纯统计 + 模板文字。"""
    findings = []
    for m in metrics[:6]:
        direction = "上升" if (m["change_pct"] or 0) > 0 else "下降"
        pct = f"{abs(m['change_pct']):.1f}%" if m["change_pct"] is not None else "—"
        findings.append(f"{m['name']}: 从 {m['first']} {direction}到 {m['last']}（{pct}），"
                        f"{'最优' if m['higher_better'] else '最低'} {m['best']}")
    ok = status == "finished"
    return {
        "verdict": "证据不足" if (not ok or not metrics) else "部分支持",
        "one_liner": (f"实验跑完，采集到 {len(metrics)} 条指标曲线" if ok
                      else "实验未正常结束，暂无有效结论"),
        "hypothesis_results": [],
        "key_findings": findings or ["本次运行没有解析到任何指标，检查代码是否按协议输出 @@METRIC"],
        "comparison": "未做自动对比（LLM 未配置，仅输出统计结果）",
        "anomalies": [] if ok else [f"运行状态：{status}"],
        "limitations": [f"自动总结降级为规则模式：{err or '未知原因'}"],
        "next_steps": ["配置 LLM API Key 以获得语义级结论分析", "检查指标协议输出是否正确"],
        "paper_paragraph": "（LLM 未配置，未生成论文段落）",
    }
