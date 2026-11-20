"""AI 功能评测集与回归看板（产品建议3 P0-37）。

第一版先落一个不依赖外部 LLM 的规则评测底座：每次运行会执行
内置 golden cases，把通过率、耗时和明细写入 ai_eval_runs。
后续可以把同一套框架扩展到模型裁判 / LLM 输出评测。
"""
import time

from core import db

MODEL = "local-rules-v1"
PROMPT_VERSION = "2026-11-20"

SEED_CASES = [
    {
        "category": "diagnosis", "name": "diag_cuda_oom",
        "payload": {
            "log": "Epoch 3: torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
            "exit_code": 1, "status": "failed", "expect": "CUDA OOM"
        },
        "expected": {"signature": "CUDA OOM"},
    },
    {
        "category": "diagnosis", "name": "diag_module_not_found",
        "payload": {
            "log": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'torchvision'",
            "exit_code": 1, "status": "failed", "expect": "ModuleNotFoundError"
        },
        "expected": {"signature": "ModuleNotFoundError"},
    },
    {
        "category": "verification", "name": "verify_unverified_number",
        "payload": {
            "summary": {"paper_paragraph": "方法准确率提升到 0.99。"},
            "metrics": [{"name": "accuracy", "first": 0.71, "last": 0.82, "best": 0.84, "worst": 0.71, "last_step": 100}],
            "expect": "0.99"
        },
        "expected": {"token": "0.99"},
    },
    {
        "category": "trust", "name": "trust_inferred_language",
        "payload": {"text": "建议增加随机种子，可能提升结论稳健性。", "expect": "inferred"},
        "expected": {"tier": "inferred"},
    },
    {
        "category": "security", "name": "redact_api_key",
        "payload": {"text": "api_key=sk-abcdefghijklmnop and password=secret123", "expect": "sk-abcdefghijklmnop"},
        "expected": {"secret": "sk-abcdefghijklmnop"},
    },
]


def ensure_seed(uid=None):
    return db.seed_ai_eval_cases(SEED_CASES, uid=uid)


def _run_case(case):
    payload = case.get("payload") or {}
    expected = case.get("expected") or {}
    name = case.get("name")

    if name.startswith("diag_"):
        from core import diagnose
        result = diagnose.diagnose(payload.get("log", ""), payload.get("exit_code"),
                                   payload.get("status"))
        actual = (result or {}).get("signature", "")
        want = expected.get("signature", "")
        return actual == want, f"signature={actual or 'None'}"

    if name == "verify_unverified_number":
        from core import verify
        result = verify.verify_numbers(payload.get("summary") or {}, payload.get("metrics") or [])
        tokens = [x.get("token") for x in result]
        want = expected.get("token", "")
        return want in tokens, f"unverified={tokens[:5]}"

    if name == "trust_inferred_language":
        from core import ai_trust
        actual = ai_trust.classify(payload.get("text", ""))
        want = expected.get("tier", "")
        return actual == want, f"tier={actual}"

    if name == "redact_api_key":
        from core import crypto
        actual = crypto.redact(payload.get("text", ""))
        secret = expected.get("secret", "")
        return bool(secret) and secret not in actual, "secret masked" if secret not in actual else "secret leaked"

    return False, "unknown case"


def run(uid=None):
    """跑一轮回归，返回汇总结果。"""
    ensure_seed(uid=uid)
    cases = [c for c in db.list_ai_eval_cases(uid=uid) if c.get("enabled")]
    scores = []
    for case in cases:
        started = time.time()
        try:
            passed, detail = _run_case(case)
        except Exception as e:
            passed, detail = False, f"error: {e}"
        scores.append({
            "id": case["id"],
            "name": case["name"],
            "category": case["category"],
            "passed": bool(passed),
            "detail": str(detail)[:300],
            "ms": int((time.time() - started) * 1000),
        })
    passed = sum(1 for s in scores if s["passed"])
    db.add_ai_eval_run(MODEL, PROMPT_VERSION, scores, passed, len(scores), uid=uid)
    return {
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "passed": passed,
        "total": len(scores),
        "scores": scores,
    }


def latest(uid=None, limit=10):
    ensure_seed(uid=uid)
    return db.list_ai_eval_runs(uid=uid, limit=limit)
