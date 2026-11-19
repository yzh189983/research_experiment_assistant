"""失败自动诊断：错误签名库 + 规则匹配，给出原因与建议动作。

不依赖 LLM，纯规则，失败时立即可用；可选让 LLM 在规则无命中时补充解读。
"""
import re

# (名称, 正则, 原因, 建议动作列表, 严重度)
SIGNATURES = [
    ("CUDA OOM", re.compile(r"CUDA out of memory|torch\.OutOfMemoryError", re.I),
     "GPU 显存不足",
     ["降低 batch size", "使用梯度累积替代大 batch", "推理时加 torch.no_grad()", "清理缓存 torch.cuda.empty_cache()"],
     "high"),
    ("ModuleNotFoundError", re.compile(r"ModuleNotFoundError:\s*No module named ['\"]([\w.]+)['\"]", re.I),
     "缺少 Python 依赖包",
     ["在 requirements.txt 补上缺失包后重跑", "检查包名拼写与大小写", "确认远程 venv 已安装依赖"],
     "high"),
    ("ImportError", re.compile(r"ImportError:\s*(cannot import name .+?|No module named .+)", re.I),
     "模块导入失败",
     ["检查包名/版本是否与本地一致", "确认依赖版本约束", "检查循环导入"],
     "medium"),
    ("OOM killed (137)", re.compile(r"(?:^|\n).*(?:Killed process|Out of memory: Kill).*", re.I),
     "进程被系统 OOM killer 杀掉（退出码 137）",
     ["降低 batch size 或模型规模", "换更大内存的机器", "减少 DataLoader 的 num_workers", "检查内存泄漏"],
     "high"),
    ("Connection refused", re.compile(r"Connection refused|ConnectionResetError|ECONNRESET", re.I),
     "网络连接被拒绝/重置",
     ["检查服务地址与端口是否正确", "确认目标服务已启动", "检查防火墙/安全组"],
     "medium"),
    ("No such file", re.compile(r"FileNotFoundError|No such file or directory", re.I),
     "文件路径不存在",
     ["检查数据路径是否正确（远程与本地可能不同）", "确认文件已上传到远程", "用绝对路径"],
     "high"),
    ("Permission denied", re.compile(r"PermissionError|Permission denied", re.I),
     "权限不足",
     ["检查文件/目录读写权限", "确认 SSH 用户对工作目录有写权限"],
     "medium"),
    ("KeyError", re.compile(r"KeyError:\s*['\"]([\w.]+)['\"]", re.I),
     "字典键不存在",
     ["检查数据字段名是否与代码一致", "用 .get() 避免缺失键崩溃"],
     "low"),
    ("AssertionError", re.compile(r"AssertionError", re.I),
     "断言失败",
     ["检查输入数据是否满足假设", "查看断言附近的条件"],
     "low"),
    ("SyntaxError", re.compile(r"SyntaxError:", re.I),
     "Python 语法错误",
     ["检查报错行附近的语法", "确认 Python 版本兼容"],
     "high"),
    ("timeout", re.compile(r"TimeoutError|timed out|Read timed out", re.I),
     "操作超时",
     ["增大超时时间", "检查网络稳定性", "减少单次处理数据量"],
     "medium"),
    ("CUDA unavailable", re.compile(r"CUDA is not available|No CUDA-capable device", re.I),
     "未检测到可用 GPU",
     ["确认机器有 GPU 且驱动正常", "检查 CUDA_VISIBLE_DEVICES", "代码加 CPU 降级分支"],
     "medium"),
]


def diagnose(log_tail, exit_code=None, status=None):
    """根据日志尾部与退出码生成诊断卡。返回 dict 或 None。"""
    text = str(log_tail or "")
    lines = text.split("\n")
    hits = []
    # 从后往前找（优先命中最近的报错）
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        for name, pattern, cause, fixes, severity in SIGNATURES:
            m = pattern.search(line)
            if m and not any(h["signature"] == name for h in hits):
                hits.append({
                    "signature": name,
                    "cause": cause,
                    "fixes": list(fixes),
                    "severity": severity,
                    "line_no": idx + 1,
                    "line": line.strip()[:300],
                    "match": m.group(1) if m.groups() else "",
                })
        if len(hits) >= 3:
            break

    # exit 137 即使日志无关键字也判 OOM
    if str(exit_code) == "137" and not any(h["signature"].startswith("OOM") for h in hits):
        hits.insert(0, {
            "signature": "OOM killed (137)", "cause": "进程被系统 OOM 杀掉（退出码 137）",
            "fixes": ["降低 batch size", "换更大内存的机器", "检查内存泄漏"],
            "severity": "high", "line_no": 0, "line": "exit_code=137", "match": "",
        })

    if not hits:
        return None
    top = hits[0]
    return {
        "signature": top["signature"],
        "cause": top["cause"],
        "severity": top["severity"],
        "fixes": top["fixes"],
        "evidence": {"line_no": top["line_no"], "line": top["line"]},
        "all_hits": hits,
        "confidence": "high" if top["severity"] == "high" else "medium",
    }


def diagnose_run(run_id):
    """从 run 记录读取 log_tail/exit_code/status 生成诊断。"""
    from core import db
    run = db.q1("SELECT log_tail, exit_code, status FROM runs WHERE id=?", (run_id,))
    if not run:
        return None
    return diagnose(run["log_tail"], run.get("exit_code"), run.get("status"))
