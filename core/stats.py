"""多 run 统计汇总与显著性检验（纯标准库实现）。

- 对同一实验的多个 finished run 计算 mean ± std、95% 置信区间
- 两组成对/独立样本比较：Welch t 检验（用正态近似给 p 值，避免引入 scipy）与
  Mann-Whitney U（小样本正态近似）
- 单一指标的多 run 汇总与"是否达到判据"的统计版判定
"""
import math
import statistics

from core import db


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def _normal_sf(z):
    """标准正态生存函数 P(Z > z)，用 erf 近似。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _t_p_two_sided(t, df):
    """t 分布双尾 p 值（正态近似，df 大时准确；小样本保守提示）。"""
    if df <= 0 or not math.isfinite(t):
        return 1.0
    # 用 Cornish-Fisher 修正的正态近似
    z = abs(t)
    try:
        correction = 1 - 1 / (4 * df)
        z = z * correction
    except Exception:
        pass
    return min(1.0, 2 * _normal_sf(z))


def _welch_t(a, b):
    """Welch t 检验：返回 (t, df, p)。"""
    if len(a) < 2 or len(b) < 2:
        return 0.0, 0.0, 1.0
    ma, mb = _mean(a), _mean(b)
    va, vb = _std(a) ** 2, _std(b) ** 2
    na, nb = len(a), len(b)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return 0.0, 0.0, 1.0
    t = (ma - mb) / math.sqrt(se2)
    df = se2 ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    return t, df, _t_p_two_sided(t, df)


def _mann_whitney_u(a, b):
    """Mann-Whitney U（正态近似，含并列校正）：返回 (U, z, p)。"""
    na, nb = len(a), len(b)
    if not na or not nb:
        return 0.0, 0.0, 1.0
    combined = [(v, 0) for v in a] + [(v, 1) for v in b]
    combined.sort(key=lambda x: x[0])
    ranks = [0.0] * len(combined)
    i = 0
    tie_term = 0.0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        t_len = j - i + 1
        tie_term += t_len ** 3 - t_len
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = r1 - na * (na + 1) / 2.0
    mu = na * nb / 2.0
    n = na + nb
    sigma2 = (na * nb / 12.0) * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0.0
    if sigma2 <= 0:
        return u1, 0.0, 1.0
    z = (u1 - mu) / math.sqrt(sigma2)
    p = min(1.0, 2 * _normal_sf(abs(z)))
    return u1, z, p


def metric_values(metric_name, runs, aggregate="last"):
    """从多个 run 的 metrics_json 中取某指标的代表值。"""
    vals = []
    for r in runs:
        try:
            import json
            data = json.loads(r.get("metrics_json") or "[]")
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        series = [m for m in data if isinstance(m, dict) and m.get("name") == metric_name]
        if not series:
            continue
        nums = [float(m["value"]) for m in series if m.get("value") is not None]
        if not nums:
            continue
        if aggregate == "best":
            vals.append(max(nums) if _higher_better(metric_name) else min(nums))
        else:
            vals.append(nums[-1])
    return vals


def _higher_better(name):
    n = str(name).lower()
    return any(k in n for k in ("acc", "f1", "auc", "bleu", "rouge", "em", "score", "reward"))


def summarize_runs(metric_name, runs, aggregate="last"):
    """单组多 run 的 mean±std、95% CI、n。"""
    vals = metric_values(metric_name, runs, aggregate=aggregate)
    if not vals:
        return {"metric": metric_name, "n": 0}
    mean, std, n = _mean(vals), _std(vals), len(vals)
    ci = 1.96 * std / math.sqrt(n) if n > 1 else 0.0
    return {
        "metric": metric_name, "n": n, "mean": round(mean, 6),
        "std": round(std, 6), "ci95": round(ci, 6),
        "min": round(min(vals), 6), "max": round(max(vals), 6),
        "values": [round(v, 6) for v in vals],
        "higher_better": _higher_better(metric_name),
    }


def compare_groups(metric_name, runs_a, runs_b, aggregate="last"):
    """两组比较：Welch t + Mann-Whitney U，返回字典。"""
    a = metric_values(metric_name, runs_a, aggregate=aggregate)
    b = metric_values(metric_name, runs_b, aggregate=aggregate)
    if len(a) < 2 or len(b) < 2:
        return {"ok": False, "reason": "样本量不足（每组至少需要 2 次运行）",
                "n_a": len(a), "n_b": len(b)}
    t, df, p_t = _welch_t(a, b)
    u, z, p_u = _mann_whitney_u(a, b)
    return {
        "ok": True, "metric": metric_name,
        "n_a": len(a), "n_b": len(b),
        "mean_a": round(_mean(a), 6), "std_a": round(_std(a), 6),
        "mean_b": round(_mean(b), 6), "std_b": round(_std(b), 6),
        "welch_t": round(t, 4), "welch_df": round(df, 2), "p_t": round(p_t, 6),
        "mannwhitney_u": round(u, 4), "p_u": round(p_u, 6),
        "significant": min(p_t, p_u) < 0.05,
        "note": "p 值为近似计算（t 用正态修正、U 用正态近似），小样本请谨慎解读；建议每组 ≥5 run。",
    }


def experiment_stats(eid, aggregate="last"):
    """某实验所有完成 run 的各指标汇总。"""
    runs = [r for r in db.list_runs(eid) if r.get("status") == "finished"]
    names = set()
    for r in runs:
        try:
            import json
            data = json.loads(r.get("metrics_json") or "[]")
            if isinstance(data, list):
                names.update(m.get("name") for m in data if isinstance(m, dict) and m.get("name"))
        except Exception:
            continue
    return {
        "n_runs": len(runs),
        "metrics": {name: summarize_runs(name, runs, aggregate=aggregate) for name in sorted(names)},
        "warning": "样本量不足（n<5），统计结论请谨慎" if len(runs) < 5 else "",
    }
