"""实验方案 → 可执行代码文件集。

生成约束（很重要，决定实验能不能无人值守跑完）：
1. 必须有 main.py 入口，且完全无交互（不 input()、不等确认）。
2. 所有指标必须用 `@@METRIC name=value step=n` 单行上报，监控端靠它画曲线。
3. 必须自动检测 GPU，没有就用 CPU；默认小规模，可用环境变量放大。
4. 数据拿不到时必须降级到合成数据继续跑完，不能直接崩。
5. 异常要打印完整 traceback 并以非零码退出，方便判失败。
"""
import json
from core import llm, planner

SYSTEM = """你是资深科研工程师（ML/CV/NLP 方向），负责把实验方案写成可以直接在远程服务器上
无人值守跑完的 Python 代码。你的代码必须一次跑通，宁可规模小也不要跑不动。只输出 JSON。"""

PROMPT = """请把下面这份实验方案实现成可执行的代码。

<实验方案>
{plan}
</实验方案>

<目标机器>
- 操作系统：Linux
- Python：{python_bin}（会创建 venv 安装 requirements.txt）
- GPU：{gpu_hint}
- 网络：可能无法访问外网下载大模型权重；公开小数据集通常可下载
</目标机器>

硬性要求（违反任何一条都算失败）：
1. 必须有 main.py 作为入口，`python main.py` 直接跑完全程，不允许任何交互式输入。
2. 每算出一个指标，必须立刻打印一行：
   @@METRIC name=<指标英文名> value=<数值> step=<整数>
   指标名必须与方案里的 metrics.name 一致。每个 epoch/阶段都要打印，方便看曲线。
   同时用 print(..., flush=True) 确保实时输出。
3. 必须自动判断 GPU：torch.cuda.is_available() 有则用，没有自动退到 CPU。
4. 默认小规模跑完（总时长控制在 {runtime_hint} 内）：少 epoch、小数据子集、小模型。
   同时支持环境变量 SCALE=full 时放大到完整规模。
5. 数据获取必须防御性处理：下载失败 / 路径不存在 → 自动降级到合成数据（torch.randn 之类）
   并打印 warning，然后继续跑完。绝对不允许因为数据问题直接崩掉。
6. 实现方案里的 baseline 与至少一个消融（ablation），每个变体都要输出指标，
   指标名用后缀区分，例如 accuracy_baseline / accuracy_ours。
7. 全程用 try/except 包住主流程，异常时打印完整 traceback，sys.exit(1)。
8. 关键步骤打印 [stage] xxx 形式的进度，便于监控识别当前阶段。
9. 最后打印一段「结果汇总」，把每个变体的最终指标清晰列出。
10. 必须从环境变量 SEED 读取随机种子（如 os.environ.get("SEED", "42")），设置 random / numpy / torch 的随机种子并打印 [env] seed=...；保证同一 run 可按 seed 复现。

输出 JSON：
{{
  "files": [
    {{"path": "main.py", "content": "完整代码，注意 JSON 里换行用 \\n 转义"}},
    {{"path": "requirements.txt", "content": "每行一个依赖，尽量少而稳"}}
  ],
  "entry": "main.py",
  "run_cmd": "python main.py",
  "requirements": "主要依赖简述",
  "notes": "实现要点 / 与方案的偏差说明（2-4 句）"
}}

代码要简洁、可读、有中文注释，不要过度设计。只输出 JSON。"""


def generate_code(plan, python_bin="python3", gpu=False, feedback=None):
    metrics = ", ".join(m["name"] for m in plan.get("metrics", []))
    runtime_hint = (plan.get("implementation") or {}).get("estimated_runtime") or "15 分钟"
    prompt = PROMPT.format(
        plan=json.dumps(plan, ensure_ascii=False, indent=2),
        python_bin=python_bin,
        gpu_hint="有（优先用 GPU，但要能退回 CPU）" if gpu else "无 / 未知（必须能在 CPU 上跑完）",
        runtime_hint=runtime_hint,
    ) + f"\n\n需要上报的指标名：{metrics}。"
    if feedback:
        prompt += f"\n\n研究者的修改意见（必须采纳）：\n{feedback}"

    raw = llm.chat([{"role": "user", "content": prompt}], system=SYSTEM, temperature=0.2)
    data = llm.extract_json(raw)
    return _normalize(data, plan)


def _normalize(data, plan):
    files = []
    for f in (data.get("files") or []):
        if not isinstance(f, dict):
            continue
        path = (f.get("path") or "").strip().lstrip("/")
        content = f.get("content") or ""
        if not path or ".." in path:
            continue
        files.append({"path": path, "content": content if content.endswith("\n") else content + "\n"})

    if not any(f["path"] == "main.py" for f in files):
        raise llm.LLMError("生成的代码缺少 main.py 入口")

    # 兜底补 requirements.txt
    if not any(f["path"] == "requirements.txt" for f in files):
        files.append({"path": "requirements.txt", "content": "torch\nnumpy\n"})

    # 兜底补 run.sh（由 ssh_runner 生成，这里只保证路径约定）
    return {
        "files": files,
        "entry": data.get("entry") or "main.py",
        "run_cmd": data.get("run_cmd") or "python main.py",
        "requirements": data.get("requirements", ""),
        "notes": data.get("notes", ""),
    }


def fallback_code(plan):
    """LLM 不可用时的演示兜底：一个能跑、会输出指标的占位实验。"""
    metrics = [m["name"] for m in plan.get("metrics", [])] or ["loss"]
    m0 = metrics[0]
    code = f'''"""自动生成的占位实验（LLM 未配置时的降级方案）
指标：{", ".join(metrics)}
"""
import os, sys, random, math, traceback

random.seed(42)
STEPS = int(os.environ.get("STEPS", 30))
SCALE = os.environ.get("SCALE", "small")


def main():
    print("[stage] 初始化", flush=True)
    print(f"[init] scale={{SCALE}} steps={{STEPS}}", flush=True)

    print("[stage] 训练", flush=True)
    best = 0.0
    for step in range(1, STEPS + 1):
        cur = 1.0 / math.sqrt(step) + random.uniform(-0.02, 0.02)
        cur = max(cur, 0.0)
        print(f"@@METRIC name={m0} value={{cur:.6f}} step={{step}}", flush=True)
        if cur > best:
            best = cur
        if step % 10 == 0:
            print(f"[progress] step {{step}}/{{STEPS}}", flush=True)

    print("[stage] 汇总", flush=True)
    print(f"[result] best_{m0}={{best:.6f}}", flush=True)
    print("=== 结果汇总 ===", flush=True)
    print(f"best_{m0}: {{best:.6f}}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
'''
    return {
        "files": [
            {"path": "main.py", "content": code},
            {"path": "requirements.txt", "content": "# 无第三方依赖\n"},
        ],
        "entry": "main.py",
        "run_cmd": "python main.py",
        "requirements": "无",
        "notes": "LLM 未配置，这是占位实验代码，仅用于验证「跑通 → 监控 → 总结」链路。",
        "fallback": True,
    }
