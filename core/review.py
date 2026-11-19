"""AI 自审 / 模拟同行评审：对生成的总结做整体审视，指出漏洞与待补实验。

作为独立的一次 LLM 调用，结果存 runs.review_json，不影响主总结流程。
"""
import json

from core import db, llm

SYSTEM = """你是一位严谨的机器学习领域审稿人（reviewer #2）。你的任务是审阅一份实验总结，
找出其中站不住脚的地方。请务必具体、尖锐但公平，不要空泛夸奖。
只输出一个 JSON 对象，格式见用户提示，不要输出其他文字或代码块。"""

PROMPT = """请审阅以下实验总结，按维度打分并指出问题。

【创新点】
{innovation}

【实验方案】
{plan}

【实验结论（被审阅对象）】
{summary}

【结果核验信息】
{verification}

请输出 JSON（所有文字用中文）：
{{
  "scores": {{"hypothesis_closed": 0-5, "baseline_adequate": 0-5, "ablation_complete": 0-5,
              "metrics_appropriate": 0-5, "confounding_controlled": 0-5, "conclusion_calibrated": 0-5}},
  "overall": "major_revision | minor_revision | accept | reject",
  "strengths": ["优点1", "优点2"],
  "major_issues": ["主要问题1：具体说明为什么站不住", "..."],
  "minor_issues": ["次要问题1", "..."],
  "missing_experiments": ["建议补的实验1", "..."],
  "verdict": "一段简短的总体审稿意见（3-5 句）"
}}

打分标准：0=严重缺失，3=基本合格，5=充分。只输出 JSON。"""


def review_summary(exp_id, run_id, summary):
    """对总结做 AI 审稿；返回 dict（含 error 表示失败）。"""
    if not llm.is_configured():
        return {"error": "LLM 未配置，跳过 AI 自审"}
    exp = db.get_experiment(exp_id) or {}
    plan = json.loads(exp.get("plan_json") or "{}")
    try:
        raw = llm.chat_json([{"role": "user", "content": PROMPT.format(
            innovation=str(exp.get("innovation") or "")[:2000],
            plan=json.dumps(plan, ensure_ascii=False)[:4000],
            summary=json.dumps(summary, ensure_ascii=False)[:5000],
            verification=json.dumps(summary.get("verification") or {}, ensure_ascii=False)[:2000],
        )}], system=SYSTEM, temperature=0.3, max_tokens=3000)
        usage = llm.last_usage()
        if usage:
            db.add_usage("review", usage.get("model", ""),
                         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                         request_id=usage.get("request_id", ""),
                         experiment_id=exp_id, run_id=run_id)
        result = {
            "ai_generated": True,
            "notice": "AI 生成的审稿意见，仅供参考，需人工判断。",
            **raw,
        }
        db.update_run(run_id, review_json=json.dumps(result, ensure_ascii=False))
        db.add_audit(exp_id, "review", "AI 自审",
                     {"overall": result.get("overall"),
                      "major_issues": len(result.get("major_issues") or [])},
                     decision="AI 生成，需人工判断", run_id=run_id)
        return result
    except Exception as e:
        err = {"error": str(e), "ai_generated": True,
               "notice": "AI 自审失败，不影响主结论。"}
        return err
