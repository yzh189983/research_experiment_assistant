"""卡卡智能体（产品优化建议4）：实验副驾 / 项目播报 / 数据集收集。

设计原则：
- 规则驱动、离线可用；所有结论继承 AI P0 可信分级与证据绑定；
- 不执行 shell、不读密钥；写操作走项目角色校验并落审计；
- 回答找不到证据时明确说“证据不足”，不编造。
"""
import json
import re
import time

import config
from core import ai_trust, db, dataset_search, llm

KAKA_SYSTEM = """你是科研实验助手里的智能体「卡卡」，绑定在某个项目和实验上。
回答规则：
1. 只依据下面提供的实验上下文回答，不要编造未给出的指标、日志、代码或结论。
2. 用中文、简洁、像实验室里靠谱的师兄，2～4 句话说完，不要 Markdown 标题和列表。
3. 有指标或日志支撑才下判断；推测必须带「可能 / 建议 / 推断」；没有证据就直说「证据不足」。
4. 用户是 viewer 时，不提供代码细节，只说结论。
"""
KAKA_DATASET_SYSTEM = """你是科研数据集整理助手。从给定论文文本中抽取实际使用的数据集。
只输出 JSON：{"datasets":[{"name":"数据集名","version":"版本或空","evidence":"原文证据句"}]}。
不要编造文本里没出现的数据集；不确定版本就留空。"""

STAGE_LABEL = {
    "idea": "想法", "plan": "方案", "code": "代码",
    "run": "运行", "summary": "总结", "confirm": "待人工确认",
}

# 53.3 允许播报的事件白名单，避免卡片刷屏
BROADCAST_EVENTS = {
    "experiment_created": "实验创建",
    "plan_ready": "方案生成完成",
    "code_ready": "代码生成完成",
    "run_started": "运行开始",
    "run_finished": "运行完成",
    "run_failed": "运行失败",
    "criteria_passed": "判据通过",
    "criteria_failed": "判据未通过",
    "summary_ready": "总结生成",
    "share_created": "分享链接创建",
    "share_revoked": "分享链接撤销",
    "member_changed": "成员变更",
}

# 54.4 本地数据集目录：只收官方来源，未核实许可一律标注需人工确认
_DATASET_CATALOG = [
    {"name": "ImageNet", "aliases": ["ilsvrc", "imagenet-1k", "imagenet1k"],
     "keywords": ["图像分类", "视觉", "image classification", "computer vision", "cv"],
     "url": "https://www.image-net.org/", "url_type": "official", "license": "研究用途（需注册）"},
    {"name": "COCO", "aliases": ["mscoco", "coco2017"],
     "keywords": ["目标检测", "实例分割", "object detection", "caption"],
     "url": "https://cocodataset.org/", "url_type": "official", "license": "CC BY 4.0"},
    {"name": "CIFAR-10", "aliases": ["cifar10"],
     "keywords": ["小图像分类", "cifar"],
     "url": "https://www.cs.toronto.edu/~kriz/cifar.html", "url_type": "official", "license": "未知/需人工确认"},
    {"name": "CIFAR-100", "aliases": ["cifar100"],
     "keywords": ["细粒度分类"],
     "url": "https://www.cs.toronto.edu/~kriz/cifar.html", "url_type": "official", "license": "未知/需人工确认"},
    {"name": "VQAv2", "aliases": ["vqa v2", "vqa2.0"],
     "keywords": ["视觉问答", "vqa", "visual question answering"],
     "url": "https://visualqa.org/", "url_type": "official", "license": "未知/需人工确认"},
    {"name": "GQA", "aliases": [], "keywords": ["视觉推理", "场景图"],
     "url": "https://downloads.cs.stanford.edu/nlp/data/gqa/", "url_type": "official", "license": "未知/需人工确认"},
    {"name": "MIMIC-CXR", "aliases": ["mimiccxr"],
     "keywords": ["医学影像", "胸片", "chest x-ray", "医学 vqa"],
     "url": "https://physionet.org/content/mimic-cxr/", "url_type": "official", "license": "PhysioNet Credentialed"},
    {"name": "MIMIC-III", "aliases": ["mimic3"],
     "keywords": ["电子病历", "电子健康记录", "ehr", "icu"],
     "url": "https://physionet.org/content/mimiciii/", "url_type": "official", "license": "PhysioNet Credentialed"},
    {"name": "PubMedQA", "aliases": ["pubmed qa"],
     "keywords": ["医学问答", "生物医学", "pubmed"],
     "url": "https://pubmedqa.github.io/", "url_type": "official", "license": "MIT"},
    {"name": "SQuAD", "aliases": ["squad 1.1", "squad2.0"],
     "keywords": ["阅读理解", "问答", "question answering"],
     "url": "https://rajpurkar.github.io/SQuAD-explorer/", "url_type": "official", "license": "CC BY-SA 4.0"},
    {"name": "SciQ", "aliases": [], "keywords": ["科学问答", "science"],
     "url": "https://allenai.org/data/sciq", "url_type": "official", "license": "CC BY 4.0"},
    {"name": "GLUE", "aliases": ["super glue", "superglue"],
     "keywords": ["自然语言理解", "nlp benchmark"],
     "url": "https://gluebenchmark.com/", "url_type": "official", "license": "各子任务不同"},
]


def _j(text, default):
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text or "") or default
    except (TypeError, ValueError):
        return default


def _llm_cfg():
    """卡卡模型配置：单独配了就用单独的，没配的字段跟随主 LLM。"""
    all_cfg = config.load_config()
    k = all_cfg.get("kaka") or {}
    base = all_cfg.get("llm") or {}
    return {
        "base_url": k.get("base_url") or base.get("base_url") or "",
        "api_key": k.get("api_key") or base.get("api_key") or "",
        "model": k.get("model") or base.get("model") or "",
        "temperature": float(k.get("temperature") or 0.2),
        "max_tokens": int(k.get("max_tokens") or 1200),
        # 卡卡问答不走主模型 fallback，避免一次提问串行调用多个模型
        "fallback_models": [],
    }


def _llm_ready():
    k = config.load_config().get("kaka") or {}
    return k.get("enabled", True) and bool(_llm_cfg().get("api_key"))


def _llm_chat(messages, system=None, json_mode=False):
    """调用卡卡专用模型；未配置、熔断或出错时返回 None，交给规则兜底。"""
    if not _llm_ready():
        return None
    cfg = _llm_cfg()
    try:
        kw = {"cfg_override": cfg, "temperature": cfg["temperature"],
              "max_tokens": cfg["max_tokens"]}
        if json_mode:
            return llm.chat_json(messages, system=system, **kw)
        return llm.chat(messages, system=system, **kw)
    except Exception:
        return None


def _norm(text):
    return str(text or "").lower()


def _safe_url(url):
    """只接受 http(s)，避免把javascript:等危险链接写入资产库。"""
    url = str(url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


# ---------------- 52 卡卡工作台 ----------------

def _monitor_note(exp, run, events=None):
    """实验中：把数字翻译成自然语言，并给出下一步建议。"""
    if not run:
        return {"message": "还没有运行记录。卡卡建议先把方案与代码准备好，再启动一次实验。",
                "tier": "unverified", "evidence": {}}
    st = run.get("status") or "unknown"
    started = run.get("started_at") or 0
    elapsed = int(max(0, time.time() - started)) if started else 0
    tail = str(run.get("log_tail") or "").strip().splitlines()
    last = tail[-1][:120] if tail else "暂无日志"
    if st in ("running", "setup", "uploading", "pending"):
        msg = f"run #{run['id']} 已运行 {elapsed // 60} 分 {elapsed % 60} 秒，最近日志：{last}。"
        if elapsed > 3600:
            msg += " 运行时间较长，建议检查是否接近预算或超时。"
        else:
            msg += " 卡卡正在盯日志与指标，异常会第一时间提醒。"
    elif st == "finished":
        msg = f"run #{run['id']} 已正常结束。卡卡建议查看结论与判据核验结果。"
    elif st in ("failed", "lost", "stopped"):
        dg = _j(run.get("diagnosis_json"), {})
        cause = dg.get("cause") or "原因待核实"
        msg = f"run #{run['id']} 状态为 {st}，诊断指向：{cause}。卡卡建议先看失败诊断再决定重试。"
    else:
        msg = f"run #{run['id']} 当前状态 {st}。"
    evidence = {"run_id": run.get("id"), "log_line": last, "status": st}
    return {"message": msg, "tier": ai_trust.classify(msg, evidence), "evidence": evidence}


def workspace(eid, uid=None):
    """52.1 卡卡工作台：阶段 / 阻塞 / 下一步 / 监控小结。"""
    exp = db.get_experiment(eid, uid=uid)
    if not exp:
        return None
    runs = db.list_runs(eid, uid=uid)
    latest = runs[0] if runs else None
    mode = exp.get("mode") or "autonomous"

    if mode == "copilot" and exp.get("checkpoint"):
        stage = "confirm"
    elif latest and latest.get("status") in ("running", "setup", "uploading", "pending"):
        stage = "run"
    elif latest and latest.get("status") in ("finished", "failed", "lost", "stopped"):
        stage = "summary" if latest.get("summary_json") else "confirm"
    elif exp.get("code_json"):
        stage = "code"
    elif exp.get("plan_json"):
        stage = "plan"
    else:
        stage = "idea"

    blockers, actions = [], []
    if not (exp.get("innovation") or "").strip():
        blockers.append("缺少创新点")
    if not exp.get("plan_json"):
        blockers.append("实验方案尚未生成")
        actions.append({"key": "plan", "label": "让卡卡写实验方案"})
    if not exp.get("code_json"):
        blockers.append("实验脚本尚未就绪")
        actions.append({"key": "code", "label": "让卡卡写实验脚本"})
    criteria = _j(exp.get("criteria_json"), {})
    if exp.get("criteria_locked") and criteria:
        pass
    elif stage in ("code", "run", "summary"):
        blockers.append("判据未锁定，结论无法判通过")
        actions.append({"key": "criteria", "label": "去锁定预注册判据"})
    if latest and latest.get("status") == "finished" and not latest.get("summary_json"):
        actions.append({"key": "summary", "label": "让卡卡总结这次结果"})
    if stage == "run":
        actions.append({"key": "refresh", "label": "刷新卡卡监控"})
    if not actions:
        actions.append({"key": "ask", "label": "问问卡卡下一步"})

    return {
        "experiment_id": eid,
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "mode": mode,
        "blockers": blockers[:5],
        "next_actions": actions[:5],
        "monitor": _monitor_note(exp, latest),
        "latest_run": {"id": latest["id"], "status": latest.get("status")} if latest else None,
        "hint": "卡卡的判断基于当前实验记录；推断性内容需你确认，不会自动覆盖代码或报告。",
    }


def _prepare_context(eid, uid):
    exp = db.get_experiment(eid, uid=uid)
    if not exp:
        return None, "实验不存在或无权限"
    latest = db.latest_run(eid, uid=uid)
    return {"exp": exp, "latest": latest}, None


def _ask_context(uid, eid, pid, ctx):
    """把实验 / 运行 / 群聊现状拼成给模型的上下文，不附加超出记录的事实。"""
    parts = []
    exp = (ctx or {}).get("exp")
    latest = (ctx or {}).get("latest")
    if exp:
        parts.append(f"实验 #{exp['id']}：{exp.get('title') or ''}")
        innovation = str(exp.get("innovation") or "")[:800]
        if innovation:
            parts.append(f"创新点：{innovation}")
        parts.append(f"状态：{exp.get('status')}")
        plan = _j(exp.get("plan_json"), {})
        if isinstance(plan, dict) and plan:
            metrics = ", ".join(str(m.get("name") or "") for m in
                                (plan.get("metrics") or []) if isinstance(m, dict))
            parts.append(f"方案：{plan.get('title') or ''}；指标：{metrics or '无'}")
        if latest:
            parts.append(f"最近运行 run #{latest['id']} 状态 {latest.get('status')}，"
                         f"退出码 {latest.get('exit_code') or '无'}")
            tail = str(latest.get("log_tail") or "")[-1200:]
            if tail:
                parts.append(f"日志尾部：{tail}")
            dg = _j(latest.get("diagnosis_json"), {})
            if isinstance(dg, dict) and dg:
                fixes = "; ".join(str(x) for x in (dg.get("fixes") or [])[:3])
                parts.append(f"失败诊断：{dg.get('cause') or ''}；修复建议：{fixes}")
            sm = _j(latest.get("summary_json"), {})
            if isinstance(sm, dict) and sm:
                parts.append(f"已有总结：{str(sm.get('one_liner') or sm.get('verdict') or '')[:400]}")
            rows = db.metrics_series(latest["id"])[:12]
            if rows:
                parts.append("最近指标：" + ", ".join(
                    f"{r.get('name')}={r.get('value')}@step{r.get('step')}" for r in rows))
        ws = workspace(eid, uid=uid) or {}
        parts.append(f"卡卡阶段：{ws.get('stage_label')}；"
                     f"阻塞：{'、'.join(ws.get('blockers') or []) or '无'}；"
                     f"下一步：{'、'.join(a.get('label', '') for a in ws.get('next_actions') or [])}")
    if pid:
        msgs = db.list_project_messages(pid, uid=uid, limit=10)
        if msgs:
            parts.append("项目群聊最近消息：" + " | ".join(
                f"{m.get('username') or '成员'}：{str(m.get('content') or '')[:120]}"
                for m in msgs[-8:]))
    return "\n".join(parts) or "当前没有可用的实验上下文。"


def _ask_evidence(eid, pid, ctx):
    ev = {"experiment_id": eid or 0, "project_id": pid or 0}
    latest = (ctx or {}).get("latest")
    if latest:
        ev["run_id"] = latest["id"]
        rows = db.metrics_series(latest["id"])[:5]
        if rows:
            ev["metrics"] = rows
        tail = str(latest.get("log_tail") or "").strip()
        if tail:
            ev["log_line"] = tail[-160:]
    return ev


def ask(uid, question, eid=None, pid=None):
    """52.1/53.4 对话入口：模型优先，规则兜底；证据不足时直说。"""
    question = str(question or "").strip()
    if not question:
        return {"answer": "请问题目或实验编号，例如“#12 为什么失败？”。", "tier": "unverified"}
    role = db.experiment_role(db.get_experiment(eid, uid=uid), uid) if eid \
        else db.get_project_role(pid, uid=uid)
    if ("代码" in question or "脚本" in question) and not db.role_allows(role, "write"):
        return {"answer": "无权限：查看实验代码需要 editor 或以上角色。",
                "tier": "unverified", "evidence": {"role": role}}

    # 全局入口（首页 / 设置页）没有指定实验时，拿用户最近一个实验当上下文，
    # 这样右下角卡卡在任何页面都能真正调用模型，而不是只回固定提示。
    if not eid and not pid:
        recent = db.list_experiments(uid=uid)
        if recent:
            eid = recent[0]["id"]

    ctx, err = _prepare_context(eid, uid) if eid else (None, "请指定实验")
    if err and not pid:
        return {"answer": "证据不足：你还没有实验，先创建一个我才能帮你。", "tier": "unverified"}
    exp = (ctx or {}).get("exp")
    latest = (ctx or {}).get("latest")

    if _llm_ready():
        context = _ask_context(uid, eid, pid, ctx)
        answer = _llm_chat(
            [{"role": "user", "content": f"实验上下文：\n{context}\n\n问题：{question}"}],
            system=KAKA_SYSTEM)
        if answer:
            answer = answer.strip()[:1200]
            evidence = _ask_evidence(eid, pid, ctx)
            tier = ai_trust.classify(answer, evidence)
            ai_trust.record_output(eid or 0, 0, "kaka_answer", answer, evidence, uid=uid)
            return {"answer": answer, "tier": tier, "evidence": evidence,
                    "model": _llm_cfg()["model"], "source": "llm"}

    if exp and ("失败" in question or "为什么" in question or "诊断" in question):
        if latest and latest.get("status") in ("failed", "lost", "stopped"):
            dg = _j(latest.get("diagnosis_json"), {})
            cause = dg.get("cause") or "诊断尚未生成"
            fixes = dg.get("fixes") or []
            answer = f"run #{latest['id']} 失败原因指向：{cause}。"
            if fixes:
                answer += " 建议：" + "；".join(str(f) for f in fixes[:2]) + "。"
            evidence = {"run_id": latest["id"], "log_line": str(latest.get("log_tail") or "")[-160:],
                        "line_no": (dg.get("evidence") or {}).get("line_no") or 0}
            return {"answer": answer, "tier": ai_trust.classify(answer, evidence), "evidence": evidence}
        return {"answer": "证据不足：当前没有已确认的失败运行，无法解释失败原因。", "tier": "unverified"}

    if exp and ("指标" in question or "accuracy" in question.lower() or "最好" in question):
        rows = db.metrics_series(latest["id"]) if latest else []
        if rows:
            best = max(rows, key=lambda r: float(r.get("value") or 0))
            answer = (f"run #{latest['id']} 最近记录到 {best.get('name')} = "
                      f"{best.get('value')}（step {best.get('step') or '—'}）。")
            evidence = {"run_id": latest["id"], "metric": best.get("name"), "metrics": rows[:5]}
            return {"answer": answer, "tier": ai_trust.classify(answer, evidence), "evidence": evidence}
        return {"answer": "证据不足：还没有可核验的指标记录。", "tier": "unverified"}

    if exp and ("下一步" in question or "建议" in question):
        ws = workspace(eid, uid=uid) or {}
        acts = [a["label"] for a in ws.get("next_actions", [])]
        answer = "卡卡建议：" + (" → ".join(acts) if acts else "先确认方案与判据。")
        return {"answer": answer, "tier": "inferred",
                "evidence": {"stage": ws.get("stage"), "experiment_id": eid}}

    if pid and ("进展" in question or "总结" in question):
        msgs = db.list_project_messages(pid, uid=uid, limit=8)
        events = [m for m in msgs if m.get("kind") in ("kaka", "system")]
        if not events:
            return {"answer": "证据不足：项目群里还没有可总结的播报记录。", "tier": "unverified"}
        answer = "最近项目进展：" + "；".join(str(m.get("content") or "")[:60] for m in events[:5]) + "。"
        return {"answer": answer, "tier": ai_trust.classify(answer, {"sources": events[:3]}),
                "evidence": {"message_ids": [m["id"] for m in events[:5]]}}

    return {"answer": "证据不足：这个问题超出当前实验上下文，卡卡不会凭空推断。", "tier": "unverified"}


GLOBAL_SYSTEM = """你是科研实验助手「卡卡」，一个友善又靠谱的科研副驾。
你能回答任何问题：科研方法、实验设计、统计分析、论文写作、编程、工具使用，也可以陪用户闲聊。
你所在产品的能力有：把创新点拆成实验方案、生成代码、SSH 部署运行、实时监控、自动总结与核验、
Git 快照谱系、AI 可信分级、项目群聊、数据集收集、网页终端、报告导出。
回答规则：
1. 中文，简洁自然，像实验室里的师兄，2～4 句，不要用 Markdown 标题和列表。
2. 用户问通用问题就正常回答；只有提供了实验上下文才结合，不要硬扯实验。
3. 涉及事实性结论要标可信度，不确定的直说不确定，绝不编造。
4. 涉及具体实验或项目数据时，只使用上下文里给出的数据。
5. 提供了项目列表时，用户问「加入了哪些项目 / 我的角色」就直接据此回答，不要说没有相关信息。
6. 提供了服务器列表时，用户问「连了哪些服务器 / 服务器信息」就直接据此回答，不要说没有相关信息。
"""


def _global_hint(uid):
    """全局对话可用的上下文：实验 / 项目 / 服务器，只含安全字段。"""
    hint = ""
    recent = db.list_experiments(uid=uid)
    if recent:
        names = "、".join(f"#{e['id']} {e.get('title') or ''}" for e in recent[:5])
        hint = f"用户最近的实验（仅供参考，用户可能问任何问题，不要强行关联）：{names}"
    else:
        hint = "用户当前还没有创建任何实验。"
    projects = db.list_projects(uid=uid)
    if projects:
        names = "、".join(f"{p.get('name') or '未命名'}（角色 {p.get('role') or '—'}）"
                         for p in projects[:12])
        hint += ("\n" if hint else "") + f"用户加入的项目与角色：{names}"
    else:
        hint += ("\n" if hint else "") + "用户当前没有加入任何项目。"
    hosts = db.list_hosts(uid=uid)
    if hosts:
        names = "、".join(
            f"{h.get('name') or '未命名'}（{h.get('host')}:{h.get('port')}，"
            f"登录用户 {h.get('username')}{'，有 GPU' if h.get('gpu') else ''}）"
            for h in hosts[:8])
        hint += ("\n" if hint else "") + f"用户已配置的服务器：{names}"
    else:
        hint += ("\n" if hint else "") + "用户当前没有配置任何服务器，可在「服务器」页添加。"
    return hint


def ask_global(uid, question):
    """全局卡卡入口：可问任何问题，最近实验只作轻量参考，不强制绑定。"""
    question = str(question or "").strip()
    if not question:
        return {"answer": "我是卡卡，有什么想聊的？", "tier": "unverified", "source": "rule"}
    hint = _global_hint(uid)
    if _llm_ready():
        answer = _llm_chat(
            [{"role": "user", "content": (hint + "\n\n" if hint else "") + f"问题：{question}"}],
            system=GLOBAL_SYSTEM)
        if answer:
            answer = answer.strip()[:1200]
            return {"answer": answer, "tier": ai_trust.classify(answer),
                    "source": "llm", "model": _llm_cfg()["model"]}
    return {"answer": "我是卡卡，你的科研副驾。模型暂时没连上，先到「设置 → 卡卡模型」检查一下配置吧。",
            "tier": "unverified", "source": "rule"}


def _scope_parts(scope):
    scope = str(scope or "global")
    if scope.startswith("exp:"):
        try:
            return "exp", int(scope.split(":", 1)[1]), 0
        except ValueError:
            pass
    if scope.startswith("project:"):
        try:
            return "project", 0, int(scope.split(":", 1)[1])
        except ValueError:
            pass
    return "global", 0, 0


def chat(uid, question, scope="global"):
    """多轮对话：带最近历史向模型提问，回答与问题都落库；失败退回规则。"""
    question = str(question or "").strip()
    if not question:
        return {"error": "消息不能为空"}
    kind, eid, pid = _scope_parts(scope)
    conv_id = db.get_or_create_kaka_conversation(scope, uid=uid)
    history = db.list_kaka_messages(conv_id, uid=uid, limit=21)[-20:]
    db.add_kaka_message(conv_id, "user", question, uid=uid)

    if kind == "exp":
        ctx, _ = _prepare_context(eid, uid)
        system, context = KAKA_SYSTEM, _ask_context(uid, eid, None, ctx)
    elif kind == "project":
        system, context = KAKA_SYSTEM, _ask_context(uid, None, pid, None)
    else:
        system, context = GLOBAL_SYSTEM, _global_hint(uid)

    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": f"当前上下文：\n{context}\n\n问题：{question}"})

    answer, source, tier, model = "", "rule", "unverified", ""
    if _llm_ready():
        raw = _llm_chat(messages, system=system)
        if raw:
            answer = raw.strip()[:1200]
            source, model = "llm", _llm_cfg()["model"]
            tier = ai_trust.classify(answer)
    if not answer:
        if kind == "exp":
            r = ask(uid, question, eid=eid)
        elif kind == "project":
            r = ask(uid, question, pid=pid)
        else:
            r = ask_global(uid, question)
        answer = str(r.get("answer") or "")[:1200]
        tier, source = r.get("tier") or "unverified", r.get("source") or "rule"

    db.add_kaka_message(conv_id, "assistant", answer,
                        {"tier": tier, "source": source, "model": model}, uid=uid)
    return {"answer": answer, "tier": tier, "source": source, "model": model,
            "conversation_id": conv_id}


def history(uid, scope="global", limit=50):
    conv_id = db.get_or_create_kaka_conversation(scope, uid=uid)
    return [{"role": m["role"], "content": m["content"],
             "created_at": m["created_at"], "meta": _j(m.get("meta"), {})}
            for m in db.list_kaka_messages(conv_id, uid=uid, limit=limit)]


def clear_history(uid, scope="global"):
    conv_id = db.get_or_create_kaka_conversation(scope, uid=uid)
    db.clear_kaka_messages(conv_id, uid=uid)
    return {"ok": True}


# ---------------- 53 项目群聊播报 ----------------

def broadcast(pid, event, title, detail="", exp_id=0, extra=None, uid=None):
    """把关键事件写入项目群聊；非法事件或越权直接忽略，不抛错影响主流程。"""
    if event not in BROADCAST_EVENTS:
        return None
    if not db.role_allows(db.get_project_role(pid, uid=uid), "read"):
        return None
    label = BROADCAST_EVENTS[event]
    content = f"【{label}】{title}" + (f" — {detail}" if detail else "")
    payload = {"event": event, "exp_id": exp_id, **(extra or {})}
    return db.add_project_message(pid, "kaka", content[:2000], payload, uid=uid)


def _broadcast_for_exp(eid, event, detail="", uid=None):
    exp = db.get_experiment(eid, uid=uid)
    if not exp or not exp.get("project_id"):
        return None
    return broadcast(exp["project_id"], event, exp.get("title") or f"#{eid}",
                     detail, exp_id=eid, uid=uid)


def prepare_script(eid, uid=None, feedback=""):
    """52.2 入口校验：确认可写并给出卡卡将执行的阶段，实际生成由现有任务复用。"""
    exp = db.get_experiment(eid, uid=uid)
    if not exp:
        return {"error": "实验不存在或无权限"}
    if not db.role_allows(db.experiment_role(exp, uid), "write"):
        return {"error": "需要 editor 或以上角色"}
    steps = []
    if not exp.get("plan_json"):
        steps.append("plan")
    steps.append("code")
    content = f"卡卡已接手实验「{exp.get('title')}」：将从创新点生成方案与脚本，关键动作会写入审计。"
    if feedback:
        content += f" 已记录你的补充意见：{str(feedback)[:200]}"
    _broadcast_for_exp(eid, "plan_ready", "卡卡开始写实验脚本", uid=uid)
    ai_trust.record_output(eid, 0, "kaka_prepare", content,
                           {"innovation": (exp.get("innovation") or "")[:300]}, uid=uid)
    return {"ok": True, "steps": steps, "message": content,
            "tier": "inferred", "needs_confirm": exp.get("mode") == "copilot"}


def summarize_result(eid, run_id=None, uid=None):
    """52.4 实验后总结：复用现有总结器，并把带证据的结果播报到项目群。"""
    exp = db.get_experiment(eid, uid=uid)
    if not exp:
        return {"error": "实验不存在或无权限"}
    run = db.get_run(run_id, uid=uid) if run_id else db.latest_run(eid, uid=uid)
    if not run:
        return {"error": "没有可总结的运行"}
    from core import summarizer
    summary = summarizer.summarize(run["id"], eid, status=run.get("status"), force=True)
    evidence = ai_trust.build_evidence_from_summary(summary, run, exp)
    verdict = (summary or {}).get("verdict") or (summary or {}).get("one_liner") or "结果已生成"
    content = f"卡卡总结了 run #{run['id']}：{verdict}"
    ai_trust.record_output(eid, run["id"], "kaka_summary", summary, evidence, uid=uid)
    event = "run_finished" if run.get("status") == "finished" else "run_failed"
    _broadcast_for_exp(eid, event, content[:180], uid=uid)
    return {"ok": True, "summary": summary, "tier": ai_trust.classify(content, evidence),
            "evidence": evidence}


# ---------------- 54 数据集收集 ----------------

def _catalog_hits(text):
    low = _norm(text)
    hits = []
    for d in _DATASET_CATALOG:
        names = [d["name"]] + d.get("aliases", [])
        if any(_norm(n) in low for n in names) or any(k in low for k in d.get("keywords", [])):
            hits.append(d)
    return hits


def _quote_evidence(text, token, limit=180):
    for line in str(text or "").splitlines():
        if _norm(token) in _norm(line) and line.strip():
            return line.strip()[:limit]
    return ""


def collect_datasets(pid, query="", papers=None, uid=None, year_from=None, year_to=None):
    """54.1-54.4 从研究任务描述 / 论文片段中抽取数据集候选并发现官方链接。

    papers: [{"title":..., "text":...}]，离线规则匹配，不做未核实的网络断言。
    """
    if not db.role_allows(db.get_project_role(pid, uid=uid), "write"):
        return {"error": "需要项目 editor 或以上角色"}
    papers = [p for p in papers if isinstance(p, dict)] if isinstance(papers, list) else []
    # 外部检索：先搜真实论文，把标题 + 摘要并入抽取语料
    web_papers = (dataset_search.search_papers(query, year_from=year_from, year_to=year_to)
                  if dataset_search.enabled() else [])
    all_papers = papers + [{"title": p["title"], "text": p.get("abstract") or ""}
                           for p in web_papers]
    corpus = " ".join([str(query or "")] + [str(p.get("text") or "") for p in all_papers])
    hits = _catalog_hits(corpus)
    paper_title = ((str(papers[0].get("title") or "") if papers else "")
                   or (web_papers[0]["title"] if web_papers else ""))[:200]

    # 有论文语料时让大模型先抽；没模型或失败则用目录从真实摘要里兜底
    extracted = []
    if all_papers and _llm_ready():
        result = _llm_chat(
            [{"role": "user", "content": "论文文本：\n" + corpus[:8000]}],
            system=KAKA_DATASET_SYSTEM, json_mode=True)
        if isinstance(result, dict):
            for d in (result.get("datasets") or [])[:10]:
                if isinstance(d, dict) and str(d.get("name") or "").strip():
                    extracted.append(d)
    if not extracted:
        for p in web_papers:
            abstract = p.get("abstract") or ""
            for d in _catalog_hits(abstract)[:3]:
                evidence = _quote_evidence(abstract, d["name"])
                extracted.append({
                    "name": d["name"], "version": "",
                    "evidence": (evidence or f"论文《{p['title'][:80]}》摘要中提到")[:500],
                    "_paper": p["title"],
                })

    candidates, seen = [], set()
    for e in extracted:
        name = str(e.get("name") or "").strip()[:160]
        if not name or _norm(name) in seen:
            continue
        seen.add(_norm(name))
        cat = next((x for x in _catalog_hits(name) if _norm(x["name"]) == _norm(name)), None)
        # 外部链接优先，找不到再用内置官方目录
        links = dataset_search.search_dataset_links(name) if dataset_search.enabled() else []
        if links:
            link = links[0]
            url, url_type, lic = _safe_url(link["url"]), link["url_type"], link["license"]
            src = "web:" + link["source"]
            downloads = link.get("downloads") or []
        else:
            url = _safe_url((cat or {}).get("url"))
            url_type = (cat or {}).get("url_type") or "unknown"
            lic = (cat or {}).get("license") or "未知/需人工确认"
            src = "catalog" if cat else "llm"
            downloads = []
        candidates.append({
            "name": name,
            "version": str(e.get("version") or "待确认")[:80],
            "paper": str(e.get("_paper") or paper_title)[:200],
            "evidence": str(e.get("evidence") or "")[:1000] or "模型从论文文本抽取，需人工核对原文。",
            "url": url,
            "url_type": url_type,
            "license": lic,
            "status": "candidate",
            "tier": "external" if e.get("evidence") else "inferred",
            "source": src,
            "extra_links": links[1:3],
            "downloads": downloads[:3],
        })

    for d in hits[:10]:
        if _norm(d["name"]) in seen:
            continue
        evidence = _quote_evidence(corpus, d["name"])
        candidates.append({
            "name": d["name"],
            "version": "待确认",
            "paper": paper_title,
            "evidence": evidence or f"由研究任务关键词“{str(query or '')[:60]}”匹配，需人工核对原文。",
            "url": _safe_url(d.get("url")),
            "url_type": d.get("url_type") or "unknown",
            "license": d.get("license") or "未知/需人工确认",
            "status": "candidate",
            "tier": "external" if evidence else "inferred",
            "source": "catalog",
        })
    notice = ("候选由卡卡从 OpenAlex / arXiv 论文中抽取，链接来自 Hugging Face / Zenodo / 官方目录；"
              if web_papers else "候选来自内置官方目录与文本匹配；")
    return {
        "ok": True,
        "candidates": candidates[:12],
        "matched": len(candidates[:12]),
        "papers": web_papers[:5],
        "sources": (["OpenAlex", "arXiv", "Hugging Face", "内置官方目录"]
                    if dataset_search.enabled() else ["内置官方目录"]),
        "notice": notice + "版本、许可与原文页码需你核对后再确认。",
        "model": _llm_cfg()["model"] if _llm_ready() else "",
    }


def adopt_datasets(pid, candidates, uid=None):
    """把确认后的候选批量写入项目数据集资产库。"""
    if not db.role_allows(db.get_project_role(pid, uid=uid), "write"):
        return {"error": "需要项目 editor 或以上角色"}
    saved = []
    for c in (candidates or [])[:20]:
        did = db.add_project_dataset(
            pid, str((c or {}).get("name") or "").strip(), uid=uid,
            version=str(c.get("version") or ""), paper=str(c.get("paper") or ""),
            evidence=str(c.get("evidence") or ""), url=_safe_url(c.get("url")),
            url_type=str(c.get("url_type") or "") or "unknown",
            license_name=str(c.get("license") or ""),
            status="confirmed" if (c or {}).get("confirmed") else "candidate")
        if did:
            saved.append(did)
    if saved:
        broadcast(pid, "experiment_created", "数据集资产更新",
                  f"卡卡发现并收录 {len(saved)} 条数据集候选", uid=uid)
    return {"ok": True, "ids": saved}
