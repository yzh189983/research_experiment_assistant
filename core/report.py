"""实验报告导出：汇总方案、结论、核验、审计、复现配置，输出 Markdown / Word / 打印 HTML。"""
import json

from core import db

_STAGE_NAMES = {
    "experiment": "实验", "plan": "方案", "code": "代码", "criteria": "判据",
    "run": "运行", "summary": "总结", "file": "文件",
}


def _load(value, default):
    if isinstance(value, default.__class__):
        return value
    try:
        return json.loads(value or "") if default is not None else default
    except Exception:
        return default


def build(eid, limit_audits=200):
    """构建完整报告数据字典（调用方已校验归属）。"""
    exp = db.get_experiment(eid)
    if not exp:
        return None
    runs = db.list_runs(eid)
    latest = db.latest_run(eid)
    summary = None
    if latest and latest.get("summary_json"):
        summary = _load(latest["summary_json"], {})
    plan = _load(exp.get("plan_json"), {})
    criteria = _load(exp.get("criteria_json"), {})
    criteria_history = _load(exp.get("criteria_history_json"), [])
    audits = db.list_audits(eid, limit_audits)
    usage = db.usage_summary(eid)
    env = _load(latest.get("env_json"), {}) if latest else {}
    return {
        "id": eid,
        "title": exp["title"],
        "innovation": exp.get("innovation", ""),
        "status": exp.get("status", ""),
        "created_at": exp.get("created_at"),
        "mode": exp.get("mode") or "autonomous",
        "plan": plan,
        "summary": summary,
        "criteria": criteria,
        "criteria_locked": bool(exp.get("criteria_locked")),
        "criteria_locked_at": exp.get("criteria_locked_at"),
        "criteria_history": criteria_history,
        "audits": audits,
        "usage": usage,
        "env": env,
        "runs": runs,
        "latest": latest,
        "verification": (summary or {}).get("verification") or {},
    }


def process_summary(audits):
    """把审计记录压缩成一段可放进报告的"实验过程摘要"。"""
    if not audits:
        return "本次实验未记录到过程审计。"
    ordered = list(reversed(audits))
    lines = []
    for a in ordered[:40]:
        stage = _STAGE_NAMES.get(a.get("stage"), a.get("stage", ""))
        ts = db_time(a.get("ts"))
        decision = f"（{a['decision']}）" if a.get("decision") else ""
        lines.append(f"- {ts} · {stage} · {a.get('action', '')}{decision}")
    if len(ordered) > 40:
        lines.append(f"- …… 另有 {len(ordered) - 40} 条记录")
    return "\n".join(lines)


def db_time(ts):
    if not ts:
        return "—"
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _fmt(v):
    if v is None or v == "":
        return "—"
    return str(v)


def markdown(r):
    """生成完整 Markdown 报告，包含核验、审计与复现配置。"""
    s = r.get("summary") or {}
    p = r.get("plan") or {}
    v = r.get("verification") or {}
    out = [
        f"# {r['title']}", "",
        f"> {_fmt(s.get('verdict'))} — {_fmt(s.get('one_liner'))}", "",
        f"实验编号 No.{r['id']}　生成时间 {db_time(db.now())}　状态 {_fmt(r.get('status'))}", "",
    ]

    out += ["## 一、创新点", "", r.get("innovation", "").strip() or "—", ""]

    out += ["## 二、实验方案", ""]
    out += [f"**核心命题**：{_fmt(p.get('core_claim') or p.get('title'))}", ""]
    if p.get("hypothesis"):
        out += ["**可检验假设**"] + [f"- {x}" for x in p["hypothesis"]] + [""]
    if p.get("metrics"):
        out += ["**指标**"]
        for m in p["metrics"]:
            if isinstance(m, dict):
                out.append(f"- {m.get('name')}（{'越高越好' if m.get('higher_better') else '越低越好'}）{m.get('desc', '')}")
            else:
                out.append(f"- {m}")
        out.append("")
    if p.get("datasets"):
        out += ["**数据集**：" + "、".join(
            d.get("name", "") if isinstance(d, dict) else str(d) for d in p["datasets"]), ""]
    if p.get("success_criteria") or p.get("failure_criteria"):
        out += ["**原定判据**",
                f"- 成功：{_fmt(p.get('success_criteria'))}",
                f"- 失败：{_fmt(p.get('failure_criteria'))}", ""]

    out += ["## 三、预注册判据与核验", ""]
    crit = r.get("criteria") or {}
    lock_txt = "已锁定" if r.get("criteria_locked") else "未锁定"
    out += [f"判据状态：{lock_txt}" +
            (f"（锁定于 {db_time(r.get('criteria_locked_at'))}）" if r.get("criteria_locked_at") else ""), ""]
    if crit.get("success") or crit.get("failure"):
        for kind, label in (("success", "成功判据"), ("failure", "失败判据")):
            if crit.get(kind):
                out += [f"**{label}**"]
                for c in crit[kind]:
                    out.append(f"- {_fmt(c) if not isinstance(c, dict) else c}")
                out.append("")
    ccheck = (v.get("criteria") or {})
    if ccheck.get("checks"):
        out += ["**判据逐条判定**（" + _fmt(ccheck.get("summary")) + "）", ""]
        out += ["| 类型 | 判据 | 实测 | 判定 |", "|---|---|---|---|"]
        for c in ccheck["checks"]:
            desc = c.get("criterion") or (
                f"{c.get('metric', '')} {c.get('op', '')} {c.get('threshold', '')}")
            out.append(f"| {c.get('kind', '')} | {_fmt(desc)} | {_fmt(c.get('observed'))} | {_fmt(c.get('status'))} |")
        out.append("")
    if r.get("criteria_history"):
        out += ["**判据变更记录**"]
        for h in r["criteria_history"]:
            out.append(f"- {db_time(h.get('changed_at'))}：{_fmt(h.get('reason')) or '未说明原因'}")
        out.append("")

    out += ["## 四、实验结果", ""]
    if r.get("summary") is None:
        out += ["本次运行尚未生成结论。", ""]
    else:
        if s.get("hypothesis_results"):
            out += ["**假设检验**"]
            for h in s["hypothesis_results"]:
                out.append(f"- {_fmt(h.get('hypothesis'))} → {_fmt(h.get('verdict'))}：{_fmt(h.get('evidence'))}")
            out.append("")
        for label, key in (("关键发现", "key_findings"), ("异常", "anomalies"),
                           ("局限性", "limitations"), ("下一步", "next_steps")):
            if s.get(key):
                out += [f"**{label}**"] + [f"- {x}" for x in s[key]] + [""]
        if s.get("comparison"):
            out += ["**对比结论**", _fmt(s["comparison"]), ""]
        stats = s.get("metrics_stats") or []
        if stats:
            out += ["**指标汇总**", "", "| 指标 | 点数 | 起始 | 结束 | 最优 |", "|---|---|---|---|---|"]
            for m in stats:
                out.append(f"| {_fmt(m.get('name'))} | {_fmt(m.get('points'))} | "
                           f"{_fmt(m.get('first'))} | {_fmt(m.get('last'))} | {_fmt(m.get('best'))} |")
            out.append("")
        if s.get("paper_paragraph"):
            out += ["## 五、论文结果段落", "", s["paper_paragraph"], ""]

    out += ["## 六、结果核验", ""]
    if v:
        unverified = v.get("unverified_numbers") or []
        out += [f"- 实测数据点：{len(v.get('observed_claims') or [])} 条",
                f"- 未在指标中找到对应数值：{len(unverified)} 条"]
        if unverified:
            out += ["- 待人工确认的数字：" + "、".join(x.get("token", "") for x in unverified[:20])]
        out += [f"- 说明：{_fmt(v.get('interpretive_notice'))}", "",
                "**证据来源**（指标 → 原始日志行）", ""]
        for src in (v.get("evidence_sources") or [])[:12]:
            line = f"- {_fmt(src.get('metric'))} = {_fmt(src.get('value'))} @step {_fmt(src.get('step'))}"
            if src.get("lines"):
                line += "：" + "；".join(f"L{x.get('line')} {x.get('text', '')[:80]}" for x in src["lines"][:2])
            out.append(line)
        out.append("")
    else:
        out += ["尚无核验信息（结论生成后自动附带）。", ""]

    env = r.get("env") or {}
    out += ["## 七、复现实验配置", ""]
    out += [f"- 随机种子（seed）：{_fmt(env.get('seed'))}",
            f"- Python：{_fmt(env.get('python'))}",
            f"- 主机：{_fmt(env.get('host'))}",
            f"- 代码快照 commit：{_fmt((r.get('latest') or {}).get('git_sha'))}",
            f"- 采集时间：{db_time(env.get('captured_at'))}"]
    if env.get("pip_freeze"):
        out += ["", "**精确依赖（pip freeze）**", "", "```", env["pip_freeze"][:6000], "```"]
    if env.get("nvidia_smi"):
        out += ["", "**硬件 / GPU（nvidia-smi）**", "", "```", env["nvidia_smi"][:4000], "```"]
    out.append("")

    u = r.get("usage") or {}
    out += ["## 八、成本与用量", "",
            f"- LLM 调用：{u.get('calls', 0)} 次　总 token：{u.get('total_tokens', 0)}　"
            f"成本：{u.get('cost', 0)}", ""]

    out += ["## 九、实验过程摘要（审计轨迹）", "", process_summary(r.get("audits")), ""]
    return "\n".join(out)


def _docx_borders(table):
    """三线表：上下粗线、表头下细线、无竖线。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge, val, sz in (("top", "single", "16"), ("bottom", "single", "16"),
                          ("left", "nil", "0"), ("right", "nil", "0"),
                          ("insideH", "single", "4"), ("insideV", "nil", "0")):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), val)
        el.set(qn("w:sz"), sz)
        el.set(qn("w:color"), "000000")
        borders.append(el)
    tbl_pr.append(borders)


def docx_bytes(r):
    """生成论文式 Word 文档（.docx）字节流。python-docx 未安装时抛出 ImportError。"""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "DengXian"
    style.font.size = Pt(10.5)
    s = r.get("summary") or {}
    p = r.get("plan") or {}
    v = r.get("verification") or {}

    doc.add_heading(r["title"], 0)
    doc.add_paragraph(f"{_fmt(s.get('verdict'))} — {_fmt(s.get('one_liner'))}")
    doc.add_paragraph(f"实验编号 No.{r['id']}　状态 {_fmt(r.get('status'))}　"
                      f"报告生成 {db_time(db.now())}")

    doc.add_heading("一、创新点", level=1)
    doc.add_paragraph(r.get("innovation", "").strip() or "—")

    doc.add_heading("二、实验方案", level=1)
    doc.add_paragraph(f"核心命题：{_fmt(p.get('core_claim') or p.get('title'))}")
    for h in p.get("hypothesis") or []:
        doc.add_paragraph(str(h), style="List Bullet")
    if p.get("success_criteria") or p.get("failure_criteria"):
        doc.add_paragraph(f"原定判据 — 成功：{_fmt(p.get('success_criteria'))}；"
                          f"失败：{_fmt(p.get('failure_criteria'))}")

    doc.add_heading("三、预注册判据与核验", level=1)
    doc.add_paragraph("判据状态：" + ("已锁定" if r.get("criteria_locked") else "未锁定"))
    checks = (v.get("criteria") or {}).get("checks") or []
    if checks:
        t = doc.add_table(rows=1, cols=4)
        _docx_borders(t)
        for i, h in enumerate(("类型", "判据", "实测", "判定")):
            t.rows[0].cells[i].text = h
        for c in checks:
            cells = t.add_row().cells
            desc = c.get("criterion") or f"{c.get('metric', '')} {c.get('op', '')} {c.get('threshold', '')}"
            for i, val in enumerate((c.get("kind", ""), str(desc), c.get("observed"), c.get("status"))):
                cells[i].text = _fmt(val)

    doc.add_heading("四、实验结果", level=1)
    if not r.get("summary"):
        doc.add_paragraph("本次运行尚未生成结论。")
    else:
        for h in s.get("hypothesis_results") or []:
            doc.add_paragraph(f"{_fmt(h.get('hypothesis'))} → {_fmt(h.get('verdict'))}：{_fmt(h.get('evidence'))}",
                              style="List Bullet")
        for label, key in (("关键发现", "key_findings"), ("异常", "anomalies"),
                           ("局限性", "limitations"), ("下一步", "next_steps")):
            if s.get(key):
                doc.add_paragraph(label + "：")
                for x in s[key]:
                    doc.add_paragraph(str(x), style="List Bullet")
        stats = s.get("metrics_stats") or []
        if stats:
            doc.add_paragraph("指标汇总：")
            t = doc.add_table(rows=1, cols=5)
            _docx_borders(t)
            for i, h in enumerate(("指标", "点数", "起始", "结束", "最优")):
                t.rows[0].cells[i].text = h
            for m in stats:
                cells = t.add_row().cells
                for i, key in enumerate(("name", "points", "first", "last", "best")):
                    cells[i].text = _fmt(m.get(key))
        if s.get("paper_paragraph"):
            doc.add_heading("五、论文结果段落", level=1)
            doc.add_paragraph(s["paper_paragraph"])

    doc.add_heading("六、结果核验", level=1)
    unverified = v.get("unverified_numbers") or []
    doc.add_paragraph(f"实测数据点 {len(v.get('observed_claims') or [])} 条；"
                      f"未在指标中找到对应数值 {len(unverified)} 条。")
    doc.add_paragraph(_fmt(v.get("interpretive_notice")))
    for src in (v.get("evidence_sources") or [])[:12]:
        doc.add_paragraph(f"{_fmt(src.get('metric'))} = {_fmt(src.get('value'))} @step {_fmt(src.get('step'))}",
                          style="List Bullet")

    env = r.get("env") or {}
    doc.add_heading("七、复现实验配置", level=1)
    for line in (f"随机种子 seed：{_fmt(env.get('seed'))}",
                 f"Python：{_fmt(env.get('python'))}",
                 f"主机：{_fmt(env.get('host'))}",
                 f"代码快照 commit：{_fmt((r.get('latest') or {}).get('git_sha'))}"):
        doc.add_paragraph(line, style="List Bullet")
    if env.get("pip_freeze"):
        doc.add_paragraph("精确依赖（pip freeze）：")
        doc.add_paragraph(env["pip_freeze"][:6000])
    if env.get("nvidia_smi"):
        doc.add_paragraph("硬件 / GPU（nvidia-smi）：")
        doc.add_paragraph(env["nvidia_smi"][:4000])

    u = r.get("usage") or {}
    doc.add_heading("八、成本与用量", level=1)
    doc.add_paragraph(f"LLM 调用 {u.get('calls', 0)} 次，总 token {u.get('total_tokens', 0)}，"
                      f"成本 {u.get('cost', 0)}。")

    doc.add_heading("九、实验过程摘要（审计轨迹）", level=1)
    for line in process_summary(r.get("audits")).splitlines():
        doc.add_paragraph(line.lstrip("- "))

    import io
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
