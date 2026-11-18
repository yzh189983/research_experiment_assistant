"""创新点 → 结构化实验方案。

这一步不做代码，只做「把想法变成可验证的实验设计」：
假设、变量、数据集、指标、对照组、消融、预期结果、风险。
"""
from core import llm

SYSTEM = """你是一位严谨的科研实验设计师，擅长把模糊的研究想法变成可执行、可复现的实验方案。
你不写代码，只输出实验设计。你的设计必须满足：可测量、可对照、有消融、有失败判据。
输出必须是合法 JSON，不要任何解释文字。"""

PROMPT = """研究者的创新点/想法如下：

<创新点>
{innovation}
</innovation>

{feedback}

请把它拆解成一份可以直接交给工程师执行的实验方案，严格按下面 JSON 结构输出：

{{
  "title": "一句话概括这个实验（不超过 30 字）",
  "core_claim": "这个创新点要证明的核心命题，必须可被数据反驳",
  "hypothesis": "可检验的假设，写成 H1、H2…，每条都要能被实验证伪",
  "novelty": "相比已有做法，新在哪里（1-3 句）",
  "task": "具体任务类型，如 图像分类 / 医学VQA / 目标检测 / 文本生成",
  "datasets": [
    {{"name": "数据集名", "note": "规模/来源/加载方式，如 torchvision 可直接下载或需要本地路径"}}
  ],
  "metrics": [
    {{"name": "指标名(英文, 程序里用)", "desc": "含义", "higher_better": true}}
  ],
  "baselines": ["对照组/基线方法，至少一个"],
  "ablations": ["消融实验，至少一个"],
  "variables": {{"independent": "自变量", "dependent": "因变量", "controlled": "需要控制的不变量"}},
  "implementation": {{
    "framework": "建议框架及版本，如 pytorch 2.x",
    "steps": ["实现步骤，4-8 条，每条是一个可执行动作"],
    "estimated_runtime": "预计耗时，如 10 分钟 / 2 小时(GPU)"
  }},
  "success_criteria": "什么样的结果算验证成功（给具体数值门槛）",
  "failure_criteria": "什么情况下说明假设不成立",
  "risks": ["可能踩的坑，2-4 条"]
}}

要求：
- metrics 里的 name 用英文小写下划线，程序要拿它画曲线。
- 实验规模要能在单机上跑完，不要设计需要 8 卡训练一个月的方案；优先小规模可复现。
- 如果创新点信息不足，按最合理的默认假设补齐，不要反问。"""


def make_plan(innovation, feedback=None):
    fb = ""
    if feedback:
        fb = f"研究者的补充/修改意见（必须采纳）：\n<feedback>\n{feedback}\n</feedback>\n"
    raw = llm.chat_json(
        [{"role": "user", "content": PROMPT.format(innovation=innovation.strip(), feedback=fb)}],
        system=SYSTEM,
        temperature=0.3,
    )
    return _normalize(raw)


def _normalize(p):
    p = p or {}
    out = {
        "title": p.get("title") or "未命名实验",
        "core_claim": p.get("core_claim", ""),
        "hypothesis": _as_list(p.get("hypothesis")),
        "novelty": p.get("novelty", ""),
        "task": p.get("task", ""),
        "datasets": p.get("datasets") or [],
        "metrics": [],
        "baselines": _as_list(p.get("baselines")),
        "ablations": _as_list(p.get("ablations")),
        "variables": p.get("variables") or {},
        "implementation": p.get("implementation") or {},
        "success_criteria": p.get("success_criteria", ""),
        "failure_criteria": p.get("failure_criteria", ""),
        "risks": _as_list(p.get("risks")),
    }
    for m in (p.get("metrics") or []):
        if isinstance(m, str):
            out["metrics"].append({"name": _slug(m), "desc": m, "higher_better": True})
        elif isinstance(m, dict):
            out["metrics"].append({
                "name": _slug(m.get("name", "metric")),
                "desc": m.get("desc", ""),
                "higher_better": bool(m.get("higher_better", True)),
            })
    if not out["metrics"]:
        out["metrics"] = [{"name": "loss", "desc": "训练损失", "higher_better": False}]
    return out


def _as_list(v):
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def _slug(s):
    import re
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", str(s).strip().lower()).strip("_")
    return s or "metric"


def fallback_plan(innovation):
    """LLM 不可用时的降级方案：保住流程能走通，提示用户这是模板。"""
    text = (innovation or "").strip()
    head = text.split("\n")[0][:60] if text else "未命名实验"
    return {
        "title": head,
        "core_claim": text[:200],
        "hypothesis": [
            "H1：所提出的方法在目标指标上优于基线",
            "H2：去掉核心模块后指标下降，说明该模块有效",
        ],
        "novelty": "（未由 LLM 解析，以下为通用模板）",
        "task": "待明确",
        "datasets": [{"name": "合成数据", "note": "LLM 未配置，使用合成数据占位"}],
        "metrics": [
            {"name": "accuracy", "desc": "准确率", "higher_better": True},
            {"name": "loss", "desc": "损失", "higher_better": False},
        ],
        "baselines": ["基线方法（待补）"],
        "ablations": ["去掉核心模块"],
        "variables": {"independent": "是否使用所提方法", "dependent": "目标指标",
                      "controlled": "数据划分、随机种子、训练轮数"},
        "implementation": {"framework": "pytorch", "steps": [
            "准备数据与固定随机种子",
            "实现基线模型与所提方法",
            "在同一划分上训练并记录指标",
            "跑消融实验",
            "汇总对比结果",
        ], "estimated_runtime": "10 分钟"},
        "success_criteria": "所提方法目标指标相对基线提升 ≥ 2%",
        "failure_criteria": "提升小于噪声波动范围（< 0.5%）",
        "risks": ["LLM 未配置，方案为通用模板，请配置后在「设置」里重新生成"],
        "fallback": True,
    }
