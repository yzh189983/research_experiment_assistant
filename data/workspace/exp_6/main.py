"""自动生成的占位实验（LLM 未配置时的降级方案）
指标：accuracy, loss
"""
import os, sys, random, math, traceback

random.seed(42)
STEPS = int(os.environ.get("STEPS", 30))
SCALE = os.environ.get("SCALE", "small")


def main():
    print("[stage] 初始化", flush=True)
    print(f"[init] scale={SCALE} steps={STEPS}", flush=True)

    print("[stage] 训练", flush=True)
    best = 0.0
    for step in range(1, STEPS + 1):
        cur = 1.0 / math.sqrt(step) + random.uniform(-0.02, 0.02)
        cur = max(cur, 0.0)
        print(f"@@METRIC name=accuracy value={cur:.6f} step={step}", flush=True)
        if cur > best:
            best = cur
        if step % 10 == 0:
            print(f"[progress] step {step}/{STEPS}", flush=True)

    print("[stage] 汇总", flush=True)
    print(f"[result] best_accuracy={best:.6f}", flush=True)
    print("=== 结果汇总 ===", flush=True)
    print(f"best_accuracy: {best:.6f}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
