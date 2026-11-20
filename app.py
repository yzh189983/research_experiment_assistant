"""科研实验助手 —— 从创新点到实验报告的自动化流水线。

流程：创新点 → 实验方案 → 可执行代码 → SSH 部署运行 → 实时监控 → 自动总结。
"""
import itertools
import json
import os
import queue
import random
import re
import secrets
import sqlite3
import requests
import threading
import time
import traceback
from urllib.parse import urlparse

from datetime import timedelta

from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_sock import Sock
from werkzeug.security import check_password_hash

import config
from core import (ai_eval, ai_tools, auth, bus, coder, crypto, db, extract,
                  gitlineage, github, kaka, llm, monitor, planner, report, ssh_runner,
                  stats as stats_mod, summarizer, terminal, workspace)
from web.blueprints import api_bp

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_BOOT_CFG = config.load_global_config()
_SCHEME = os.environ.get("REKIT_SCHEME", _BOOT_CFG["server"].get("scheme", "http")).lower()
_FORCE_HTTPS = os.environ.get(
    "REKIT_FORCE_HTTPS",
    str(_BOOT_CFG["server"].get("force_https", _SCHEME == "https"))).lower() \
    not in ("0", "false", "no")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024      # 文稿上传上限
app.secret_key = config.get_or_create_secret_key()
app.permanent_session_lifetime = timedelta(days=_BOOT_CFG["auth"]["session_days"])
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = _SCHEME == "https"
app.config["PREFERRED_URL_SCHEME"] = _SCHEME
config.ensure_dirs()
db.init_db()
config.migrate_config_secrets()
db.migrate_host_secrets()
auth.migrate_local_token()
auth.ensure_admin()
auth.migrate_admin_password_file()

# WebSocket：网页终端用
sock = Sock(app)


# ---------------- 全局鉴权 ----------------
_API_PUBLIC = {"/api/auth/login", "/api/auth/register"}


@app.before_request
def _force_https():
    if not _FORCE_HTTPS or request.is_secure:
        return None
    if (_BOOT_CFG["security"].get("trust_proxy")
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https"):
        return None
    return redirect(request.url.replace("http://", "https://", 1), code=308)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if _SCHEME == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.before_request
def _load_user():
    """每个请求都解析登录用户并写入 config 上下文，db 层据此自动隔离。"""
    user = auth.current_user()
    if user:
        config.set_current_uid(user["id"])
    else:
        config.set_current_uid(None)
        # API 统一拦截：避免逐条路由漏加装饰器
        public_share_comment = (request.path.startswith("/api/share/")
                                and request.path.endswith("/comment"))
        if (request.path.startswith("/api/") and request.path not in _API_PUBLIC
                and not public_share_comment):
            return jsonify({"error": "未登录或登录已失效，请先执行 rekit login"}), 401


@app.context_processor
def _inject_user():
    return {"current_user": getattr(g, "_auth_user", None),
            "auth_enabled": True,
            "active": getattr(g, "active_page", "")}


@app.template_filter("timestamp")
def _ts_filter(value):
    return report.db_time(value)


@app.template_filter("dataset_name")
def _ds_filter(value):
    return value.get("name", "") if isinstance(value, dict) else str(value)


def _set_active(name):
    g.active_page = name


def own_exp_or_404(eid, action="read"):
    """校验实验可见性与项目角色权限。

    db.get_experiment 已按“本人或项目成员”过滤；这里再按动作检查
    viewer / editor / owner 的最小角色，避免越权修改、运行或删除。
    """
    exp = db.get_experiment(eid)
    if not exp:
        abort(404)
    if not db.role_allows(db.experiment_role(exp), action):
        abort(403)
    return exp


def own_project_or_404(pid, action="read"):
    project = db.get_project(pid)
    if not project:
        abort(404)
    if not db.role_allows(db.get_project_role(pid), action):
        abort(403)
    return project


def own_run_or_404(rid, action="read"):
    run = db.get_run(rid)
    if not run:
        abort(404)
    exp = db.get_experiment(run["experiment_id"])
    if exp and not db.role_allows(db.experiment_role(exp), action):
        abort(403)
    return run


def own_host_or_404(hid):
    host = db.get_host(hid)
    if not host:
        abort(404)
    return host


def own_host_or_404_safe(hid):
    """同 own_host_or_404，但返回 None 而不是抛 404（调用方要自定义错误文案）。"""
    return db.get_host(hid)


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "资源不存在或无权限访问"}), 404
    return e


@app.errorhandler(403)
def _forbidden(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "当前项目角色无权执行此操作"}), 403
    return e


# 服务端渲染状态徽章（与 static/js/app.js 的 statusBadge 保持一致）
_STATUS_MAP = {
    "draft": ("idle", "待开始"), "planned": ("idle", "方案就绪"),
    "coded": ("idle", "代码就绪"), "running": ("run", "运行中"),
    "uploading": ("run", "上传中"), "setup": ("run", "配置环境"),
    "pending": ("run", "等待中"), "done": ("ok", "已完成"),
    "finished": ("ok", "已完成"), "failed": ("err", "失败"),
    "lost": ("err", "失联"), "stopped": ("warn", "已停止"),
}


def status_badge(st):
    cls, label = _STATUS_MAP.get(st, ("idle", st or "未知"))
    return f'<span class="badge {cls}"><i class="dot"></i>{label}</span>'


app.jinja_env.globals["statusBadge"] = status_badge


# ---------------- 后台任务 ----------------

def bg(exp_id, kind, fn, *args, **kw):
    """把耗时任务（LLM 生成 / 部署）丢到后台，通过 SSE 通知前端。

    线程不继承 Flask 请求里的 ContextVar，所以在这里把 uid 传进去重设，
    否则后台线程里的 db 调用会落到默认用户、写串数据。
    """
    uid = config.current_uid()

    def runner():
        with config.as_user(uid):
            bus.publish(exp_id, {"type": "job", "kind": kind, "state": "started"})
            try:
                fn(exp_id, *args, **kw)
                bus.publish(exp_id, {"type": "job", "kind": kind, "state": "done"})
            except Exception as e:
                print(crypto.redact(traceback.format_exc()))
                msg = crypto.redact(f"{kind} 失败：{e}")
                bus.publish(exp_id, {"type": "job", "kind": kind, "state": "error", "error": msg})
                bus.note(exp_id, msg, "error")
                db.add_event(exp_id, None, msg, kind="error", level="error")

    threading.Thread(target=runner, daemon=True).start()


def _record_llm_usage(stage, exp_id, run_id=None):
    usage = llm.last_usage()
    if not usage:
        return
    db.add_usage(stage, usage.get("model", ""), usage.get("prompt_tokens", 0),
                 usage.get("completion_tokens", 0),
                 request_id=usage.get("request_id", ""),
                 experiment_id=exp_id, run_id=run_id)


def _job_plan(exp_id, feedback=None):
    exp = db.get_experiment(exp_id)
    bus.note(exp_id, "正在解析创新点，生成实验方案…")
    try:
        plan = planner.make_plan(exp["innovation"], feedback=feedback)
    except llm.LLMError as e:
        if "还没配置" in str(e) or "API Key" in str(e):
            plan = planner.fallback_plan(exp["innovation"])
            bus.note(exp_id, "LLM 未配置，已用通用模板生成方案（配置后可重新生成）", "warn")
        else:
            raise
    _record_llm_usage("plan", exp_id)
    db.update_experiment(exp_id, plan_json=json.dumps(plan, ensure_ascii=False),
                         status="planned", title=exp["title"])
    try:
        from core import ai_trust
        ai_trust.record_output(exp_id, None, "plan", plan,
                               {"experiment_id": exp_id}, uid=exp.get("user_id"))
    except Exception:
        pass
    db.add_audit(exp_id, "plan", "生成方案",
                 {"before": exp.get("plan_json") or "", "after": plan.get("title") or "",
                  "feedback": feedback or ""}, decision="已生成")
    if exp.get("mode") == "copilot":
        db.update_experiment(exp_id, checkpoint="code")
    bus.note(exp_id, f"方案已生成：{plan['title']}")
    db.add_event(exp_id, None, f"实验方案已生成：{plan['title']}", kind="stage")


def _job_code(exp_id, feedback=None, host_id=None):
    exp = db.get_experiment(exp_id)
    plan = json.loads(exp.get("plan_json") or "{}")
    if not plan:
        raise ValueError("请先生成实验方案")
    host = db.get_host(host_id) if host_id else None
    bus.note(exp_id, "正在编写实验代码…")
    try:
        code = coder.generate_code(plan, python_bin=(host or {}).get("python_bin", "python3"),
                                   gpu=bool((host or {}).get("gpu")), feedback=feedback)
    except llm.LLMError as e:
        if "API Key" in str(e) or "还没配置" in str(e):
            code = coder.fallback_code(plan)
            bus.note(exp_id, "LLM 未配置，已生成占位实验代码（可手动替换）", "warn")
        elif "返回为空" in str(e) or "结构不完整" in str(e):
            # 方案太简单 / 模型偶发空返回：仍给一份可跑的占位代码，避免按钮“没反应”
            code = coder.fallback_code(plan)
            bus.note(exp_id, f"{e}；已生成占位代码，可补全方案后点「重新生成代码」", "warn")
        else:
            raise
    _record_llm_usage("code", exp_id)
    old_files = [f.get("path") for f in json.loads(exp.get("code_json") or "{}").get("files", [])]
    workspace.write_files(exp_id, code)
    db.update_experiment(exp_id, code_json=json.dumps(code, ensure_ascii=False), status="coded")
    try:
        from core import ai_trust
        ai_trust.record_output(exp_id, None, "code",
                               {"files": [f["path"] for f in code.get("files", [])],
                                "entry": code.get("entry", ""), "notes": code.get("notes", "")},
                               {"experiment_id": exp_id}, uid=exp.get("user_id"))
    except Exception:
        pass
    db.add_audit(exp_id, "code", "生成代码",
                 {"before_files": old_files,
                  "after_files": [f.get("path") for f in code.get("files", [])],
                  "feedback": feedback or ""}, decision="已生成")
    if exp.get("mode") == "copilot":
        db.update_experiment(exp_id, checkpoint="run")
    bus.note(exp_id, f"代码已生成，共 {len(code['files'])} 个文件")
    db.add_event(exp_id, None, f"代码已生成：{', '.join(f['path'] for f in code['files'])}", kind="stage")


def _job_run(exp_id, run_id, host_id, code, seed=42):
    ssh_runner.deploy_and_launch(run_id, host_id, exp_id, code, seed=seed)


# ---------------- 登录 / 注册 ----------------

def _registration_enabled():
    return bool(config.load_global_config()["auth"].get("allow_registration", True))


def _safe_next(nxt):
    nxt = nxt or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index")


def _safe_url(value):
    value = str(value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) else ""


_PROFILE_FIELDS = (
    "name", "title", "affiliation", "research_fields", "keywords",
    "bio", "skills", "education", "experience", "email",
    "orcid", "scholar", "github", "website",
)

EXPERIMENT_TEMPLATES = [
    {"id": "classifier", "name": "训练分类器",
     "innovation": "在小规模数据集上训练一个分类器，比较基线方法与改进方法的准确率，并给出可复现的指标曲线。"},
    {"id": "lora", "name": "LoRA 微调",
     "innovation": "对一个小模型做 LoRA 微调，比较不同 rank 下的效果与训练开销，验证参数高效微调的有效性。"},
    {"id": "grid", "name": "超参网格",
     "innovation": "对关键超参数做小型网格搜索，记录每组参数的核心指标，选出最优配置并说明稳定性。"},
    {"id": "ablation", "name": "消融实验",
     "innovation": "围绕核心方法做消融实验，逐项移除关键模块并比较指标变化，验证每个模块的贡献。"},
]


def _create_account(username, password, role="member"):
    username = str(username or "").strip()
    password = str(password or "")
    if not 2 <= len(username) <= 32:
        return None, username, "用户名长度需为 2-32 个字符"
    if not 6 <= len(password) <= 128:
        return None, username, "密码长度需为 6-128 位"
    if db.get_user_by_name(username):
        return None, username, "用户名已存在"
    try:
        uid = db.add_user(username, auth.hash_password(password), role=role)
    except sqlite3.IntegrityError:
        return None, username, "用户名已存在"
    return uid, username, ""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user_ok():
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = db.get_user_by_name(username)
        if user and auth.verify_password(user, password):
            auth.login_user(user["id"], remember=bool(request.form.get("remember")))
            nxt = request.args.get("next") or request.form.get("next") or ""
            return redirect(_safe_next(nxt))
        error = "用户名或密码错误"
    return render_template("login.html", error=error,
                           next=request.args.get("next", ""),
                           can_register=_registration_enabled())


def current_user_ok():
    return auth.current_user() is not None


@api_bp.post("/api/auth/login")
def api_auth_login():
    """供 CLI/脚本登录：校验账号密码，返回可持久化的 API token。"""
    d = request.get_json(force=True) or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    user = db.get_user_by_name(username)
    if not user or not auth.verify_password(user, password):
        return jsonify({"error": "用户名或密码错误"}), 401
    # 同时建立 session（网页 API 调用）与 token（CLI/脚本），两者都可用
    auth.login_user(user["id"])
    token = auth.issue_token(user["id"])
    return jsonify({"token": token, "user": {"id": user["id"],
                                              "username": user["username"],
                                              "role": user["role"]}})


@api_bp.get("/api/auth/me")
def api_auth_me():
    u = auth.current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    return jsonify({"id": u["id"], "username": u["username"], "role": u["role"]})


@api_bp.post("/api/auth/token")
def api_auth_token():
    """已登录（session）时签发/轮换 token，供网页端拿去配 CLI。"""
    u = auth.current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    return jsonify({"token": auth.issue_token(u["id"])})


@api_bp.post("/api/auth/register")
def api_auth_register():
    """自助注册成员账号；可通过 auth.allow_registration 关闭。"""
    if not _registration_enabled():
        return jsonify({"error": "注册已关闭，请联系管理员创建账号"}), 403
    d = request.get_json(force=True) or {}
    uid, username, err = _create_account(d.get("username"), d.get("password"))
    if err:
        return jsonify({"error": err}), (409 if err == "用户名已存在" else 400)
    return jsonify({"token": auth.issue_token(uid), "user": {
        "id": uid, "username": username, "role": "member"}})


@app.post("/logout")
def logout_page():
    auth.logout_user()
    return redirect(url_for("login_page"))


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if current_user_ok():
        return redirect(url_for("index"))
    if not _registration_enabled():
        return redirect(url_for("login_page"))
    error = ""
    nxt = request.form.get("next") or request.args.get("next", "")
    if request.method == "POST":
        username = request.form.get("username") or ""
        password = request.form.get("password") or ""
        if password != (request.form.get("confirm") or ""):
            error = "两次输入的密码不一致"
        else:
            uid, username, error = _create_account(username, password)
            if not error:
                auth.login_user(uid)
                return redirect(_safe_next(nxt))
    return render_template("register.html", error=error, next=nxt)


# ---------------- 页面 ----------------

@app.route("/")
@auth.login_required
def index():
    _set_active("exp")
    exps = db.list_experiments()
    busy = sum(1 for e in exps if e["status"] in
               ("running", "setup", "uploading", "pending"))
    done = sum(1 for e in exps if e["status"] in ("finished", "done"))
    stats = {"exp": len(exps), "busy": busy, "done": done,
             "hosts": len(db.list_hosts())}
    recent = db.recent_runs(8)
    failed = [r for r in recent if r["status"] in ("failed", "lost", "stopped")]
    return render_template("index.html", experiments=exps,
                           hosts=db.list_hosts(), stats=stats,
                           recent_runs=recent, failed_runs=failed,
                           timeline=db.global_timeline(30),
                           templates=EXPERIMENT_TEMPLATES,
                           projects=db.list_projects(),
                           llm_ok=llm.is_configured())


@app.route("/projects")
@auth.login_required
def projects_page():
    _set_active("projects")
    show_archived = request.args.get("archived") == "1"
    projects = db.list_projects(include_archived=show_archived)
    enriched = []
    for p in projects:
        d = dict(p)
        d["members"] = db.list_project_members(p["id"])
        d["experiments"] = db.list_experiments(project_id=p["id"],
                                                include_archived_projects=True)
        enriched.append(d)
    return render_template("projects.html", projects=enriched,
                           show_archived=show_archived, all_users=db.list_users())


@app.route("/project/<int:pid>")
@auth.login_required
def project_detail_page(pid):
    _set_active("projects")
    own_project_or_404(pid)
    project = db.get_project(pid)
    return render_template(
        "project.html",
        project=project,
        role=db.get_project_role(pid),
        members=db.list_project_members(pid),
        experiments=db.list_experiments(project_id=pid, include_archived_projects=True),
        projects=db.list_projects(),
        all_users=db.list_users())


@app.route("/exp/<int:eid>")
@auth.login_required
def exp_page(eid):
    _set_active("exp")
    exp = db.get_experiment(eid)
    if not exp:
        return redirect(url_for("index"))
    perm = db.experiment_permissions(exp)
    project = db.get_project(exp.get("project_id") or 0)
    latest = db.latest_run(eid)
    boot = {
        "permissions": perm,
        "project": {"id": project["id"], "name": project["name"]} if project else None,
        "id": eid,
        "title": exp["title"],
        "innovation": exp["innovation"],
        "status": exp["status"],
        "plan": json.loads(exp["plan_json"]) if exp.get("plan_json") else None,
        "code": json.loads(exp["code_json"]) if exp.get("code_json") else None,
        "files": workspace.list_files(eid),
        "latest_run": latest,
        "diagnosis": json.loads(latest.get("diagnosis_json") or "{}") if latest else {},
        "review": json.loads(latest.get("review_json") or "{}") if latest else {},
        "stats": stats_mod.experiment_stats(eid),
        "summary": json.loads(latest["summary_json"]) if (latest and latest.get("summary_json")) else None,
        "runs": db.list_runs(eid),
        "mode": exp.get("mode") or "autonomous",
        "checkpoint": exp.get("checkpoint") or "",
        "tags": exp.get("tags") or "",
        "archived": bool(exp.get("archived")),
        "criteria": json.loads(exp.get("criteria_json") or "{}"),
        "criteria_locked": bool(exp.get("criteria_locked")),
        "criteria_history": json.loads(exp.get("criteria_history_json") or "[]"),
        "audits": db.list_audits(eid, 100),
        "ai_outputs": db.list_ai_outputs(experiment_id=eid, uid=g._auth_user["id"], limit=80),
        "usage": db.usage_summary(eid),
        "server_now": db.now(),
    }
    observe_token = auth.make_observe_token(g._auth_user["id"], eid)
    return render_template("experiment.html", exp=exp, hosts=db.list_hosts(),
                           llm_ok=llm.is_configured(), boot=boot,
                           perm=perm, project=project,
                           observe_token=observe_token)


@app.get("/report/<int:eid>")
@auth.login_required
def report_page(eid):
    """论文式打印页：浏览器打印 / 另存为 PDF，含核验、审计与复现配置。"""
    _set_active("exp")
    own_exp_or_404(eid)
    data = report.build(eid)
    if data is None:
        abort(404)
    return render_template("report.html", r=data,
                           md=report.markdown(data),
                           process=report.process_summary(data.get("audits")))


def _redact_text(s):
    """脱敏模式：隐藏 IP、用户名路径、HuggingFace 缓存路径。"""
    if not s:
        return s
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]", str(s))
    s = re.sub(r"/(?:Users|home)/[^/\s]+", "/[USER]", s)
    s = re.sub(r"/root/\.cache/[^\s]+", "[CACHE]", s)
    return s


@app.get("/observe/<token>")
def observe_page(token):
    """只读观察链接：兼容旧签名 token 与增强分享链接（密码/次数/脱敏）。"""
    share = None
    pwd_ok = True
    if token.startswith("s_"):
        share = db.get_share_link(token)
        if not share or share["revoked"]:
            abort(404)
        if share["expires_at"] and share["expires_at"] < time.time():
            abort(404)
        if share["max_views"] and share["views"] >= share["max_views"]:
            abort(410)
        pwd_ok = auth.verify_share_password(request.args.get("p", ""),
                                             share["salt"], share["password_hash"])
        uid, eid = share["user_id"], share["experiment_id"]
        pid = share.get("project_id") or 0
    else:
        parsed = auth.verify_observe_token(token)
        if not parsed:
            abort(404)
        uid, eid = parsed
        pid = 0

    if pid:
        project = db.get_project(pid)
        if not project:
            abort(404)
        if pwd_ok:
            db.incr_share_views(share["id"])
        experiments = db.list_experiments(project_id=pid, include_archived_projects=True)
        return render_template("project_observe.html", project=project,
                               experiments=experiments, share=share, pwd_ok=pwd_ok,
                               token=token, redact=bool(share and share["redact"]))

    exp = db.get_experiment(eid, uid=uid)
    if not exp:
        abort(404)
    owner = db.get_user(uid)
    latest = db.latest_run(eid)
    runs = db.list_runs(eid, uid=uid)
    redact = bool(share and share["redact"])
    if redact:
        exp = dict(exp)
        exp["innovation"] = _redact_text(exp.get("innovation"))
        runs = [dict(r, log_tail=_redact_text(r.get("log_tail"))) for r in runs]
        latest = dict(latest, log_tail=_redact_text(latest.get("log_tail"))) if latest else None

    if share and pwd_ok:
        db.incr_share_views(share["id"])
        db.add_audit(eid, "share", "访问分享链接",
                     {"token": token, "redact": redact},
                     decision="已查看", uid=uid)
    return render_template("observe.html", exp=exp,
                           owner=owner["username"] if owner else "未知",
                           runs=runs, latest=latest,
                           summary=json.loads(latest["summary_json"]) if
                           (latest and latest.get("summary_json")) else None,
                           share=share, pwd_ok=pwd_ok, token=token,
                           comments=db.list_share_comments(eid, uid=uid) if share else [],
                           redact=redact)


def _fmt_num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(n)) if n.is_integer() else f"{n:g}"


def _elapsed_text(started_at, now_ts=None):
    if not started_at:
        return "—"
    secs = max(0, int((now_ts or db.now()) - float(started_at)))
    if secs < 60:
        return f"{secs}秒"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}分{secs}秒"
    hours, mins = divmod(mins, 60)
    return f"{hours}小时{mins}分"


def _progress_payload(run):
    """从日志或指标里尽力解析进度；没有明确进度时回退到阶段说明。"""
    lines = [ln.strip() for ln in (run.get("log_tail") or "").splitlines() if ln.strip()]
    for line in reversed(lines):
        m = re.search(r"\[progress\]\s*step\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
                      line, re.I)
        if not m:
            m = re.search(r"\bstep\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", line, re.I)
        if m:
            cur, total = float(m.group(1)), float(m.group(2))
            pct = max(0, min(100, round(cur / total * 100))) if total > 0 else None
            return {"percent": pct,
                    "text": f"step {_fmt_num(cur)}/{_fmt_num(total)}",
                    "source": "日志"}

        m = re.search(r"进度[:：]?\s*(\d+(?:\.\d+)?)\s*%", line)
        if m:
            pct = max(0, min(100, round(float(m.group(1)))))
            return {"percent": pct, "text": f"{pct}%", "source": "日志"}

    series = db.metrics_series(run["id"], limit=20)
    steps = [p["s"] for pts in series.values() for p in pts if p.get("s") is not None]
    if steps:
        return {"percent": None, "text": f"step {_fmt_num(max(steps))}", "source": "指标"}

    fallback = {"pending": "等待部署", "uploading": "上传代码",
                "setup": "配置环境"}.get(run.get("status"), "等待脚本输出 step 信息")
    return {"percent": None, "text": fallback, "source": "阶段"}


def _host_activity_by_host():
    """按服务器聚合当前用户的活跃 run，供服务器页展示脚本与进度。"""
    u = auth.current_user()
    if not u:
        return {}
    now_ts = db.now()
    by_host = {}
    for run in db.active_runs(uid=u["id"]):
        exp = db.get_experiment(run["experiment_id"])
        code = {}
        if exp and exp.get("code_json"):
            try:
                code = json.loads(exp["code_json"]) or {}
            except Exception:
                code = {}
        log_lines = [ln.strip() for ln in (run.get("log_tail") or "").splitlines() if ln.strip()]
        by_host.setdefault(run["host_id"], []).append({
            "run_id": run["id"],
            "exp_id": run["experiment_id"],
            "exp_title": (exp or {}).get("title") or f"实验 #{run['experiment_id']}",
            "script": code.get("entry") or "main.py",
            "status": run["status"],
            "pid": run.get("pid") or "",
            "elapsed": _elapsed_text(run.get("started_at"), now_ts),
            "progress": _progress_payload(run),
            "last_line": log_lines[-1][:160] if log_lines else "",
        })
    for items in by_host.values():
        items.sort(key=lambda x: x["run_id"], reverse=True)
    return by_host


@app.route("/hosts")
@auth.login_required
def hosts_page():
    _set_active("hosts")
    return render_template("hosts.html", hosts=db.list_hosts(),
                           active_by_host=_host_activity_by_host())


@app.route("/terminal")
@auth.login_required
def terminal_index():
    _set_active("terminal")
    return render_template("terminal_index.html", hosts=db.list_hosts(),
                           active_host=None)


@app.route("/terminal/<int:hid>")
@auth.login_required
def terminal_page(hid):
    _set_active("terminal")
    host = db.get_host(hid)
    if not host:
        return redirect(url_for("terminal_index"))
    # 只给前端展示用字段，别把密码/私钥路径带到页面上
    safe = {k: host.get(k) for k in ("id", "name", "host", "port", "username",
                                     "workdir", "python_bin", "gpu")}
    return render_template("terminal.html", host=safe, hosts=db.list_hosts(),
                           read_only=False)


@app.route("/terminal/<int:hid>/observe")
@auth.login_required
def terminal_observe_page(hid):
    _set_active("terminal")
    host = db.get_host(hid)
    if not host:
        return redirect(url_for("terminal_index"))
    safe = {k: host.get(k) for k in ("id", "name", "host", "port", "username",
                                     "workdir", "python_bin", "gpu")}
    return render_template("terminal.html", host=safe, hosts=db.list_hosts(),
                           read_only=True)


@app.route("/settings")
@auth.login_required
def settings_page():
    _set_active("settings")
    cfg = config.load_config()
    gh_configs = [{"id": r["id"], "repo_full_name": r["repo_full_name"],
                   "token_hint": r["token_hint"] or "",
                   "default_branch": r["default_branch"] or "main"}
                  for r in db.list_git_remotes()]
    uid = g._auth_user["id"]
    return render_template("settings.html", cfg=cfg,
                           api_key_masked=config.mask_secret(cfg["llm"]["api_key"]),
                           has_key=bool(cfg["llm"]["api_key"]),
                           gh_configs=gh_configs,
                           notify_cfg=cfg.get("notify") or {},
                           ai_eval_runs=ai_eval.latest(uid=uid, limit=5),
                           ai_tool_audit_count=len(db.list_ai_tool_audit(uid=uid, limit=50)),
                           kaka_cfg=cfg.get("kaka") or {},
                           kaka_effective=kaka._llm_cfg(),
                           kaka_ready=kaka._llm_ready(),
                           users=db.list_users() if g._auth_user["role"] == "admin" else [])


@app.route("/profile")
@auth.login_required
def profile_page():
    _set_active("profile")
    u = auth.current_user()
    profile = db.get_profile(u["id"])
    tags = [t.strip() for t in re.split(r"[,，、;；]", profile.get("research_fields", ""))
            if t.strip()]
    links = {k: _safe_url(profile.get(k)) for k in ("scholar", "github", "website")}
    filled = sum(1 for k in _PROFILE_FIELDS if str(profile.get(k) or "").strip())
    return render_template("profile.html", profile=profile, tags=tags, links=links,
                           filled=filled, total=len(_PROFILE_FIELDS))


# ---------------- 实验 API ----------------

@api_bp.post("/api/experiments")
def api_create_exp():
    d = request.get_json(force=True)
    title = (d.get("title") or "").strip() or "未命名实验"
    innovation = (d.get("innovation") or "").strip()
    if len(innovation) < 10:
        return jsonify({"error": "创新点描述太短，至少写 10 个字"}), 400
    try:
        pid = int(d.get("project_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    pid = pid or db.ensure_personal_project(g._auth_user["id"])
    if not db.role_allows(db.get_project_role(pid), "write"):
        return jsonify({"error": "当前项目角色无权创建实验"}), 403
    eid = db.create_experiment(title, innovation,
                               template_id=(d.get("template_id") or "").strip(),
                               project_id=pid)
    if not eid:
        return jsonify({"error": "当前项目角色无权创建实验"}), 403
    db.add_audit(eid, "experiment", "创建实验", {"title": title, "template_id": d.get("template_id") or "",
                                                "project_id": pid},
                 decision="已创建")
    db.add_event(eid, None, "实验已创建", kind="stage")
    return jsonify({"id": eid})


@api_bp.get("/api/templates")
def api_templates():
    return jsonify(EXPERIMENT_TEMPLATES)


def _expand_params(params):
    if isinstance(params, list):
        if not params:
            return [{}]
        if all(isinstance(x, dict) for x in params):
            return params
        return [{"value": x} for x in params]
    if isinstance(params, dict) and params:
        keys = list(params.keys())
        values = [v if isinstance(v, list) else [v] for v in params.values()]
        return [dict(zip(keys, combo)) for combo in itertools.product(*values)]
    return [{}]


@api_bp.post("/api/experiments/batch")
def api_batch_exp():
    d = request.get_json(force=True) or {}
    template = next((t for t in EXPERIMENT_TEMPLATES
                     if t["id"] == d.get("template_id")), None)
    base = (d.get("innovation") or "").strip() or (
        template["innovation"] if template else "")
    if len(base) < 10:
        return jsonify({"error": "创新点描述太短，至少写 10 个字"}), 400
    combos = _expand_params(d.get("params"))[:50]
    if not combos:
        return jsonify({"error": "没有可创建的参数组合"}), 400
    try:
        pid = int(d.get("project_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    pid = pid or db.ensure_personal_project(g._auth_user["id"])
    if not db.role_allows(db.get_project_role(pid), "write"):
        return jsonify({"error": "当前项目角色无权创建实验"}), 403
    prefix = (d.get("title_prefix") or (template["name"] if template else "批量实验")).strip()
    ids = []
    for idx, params in enumerate(combos, 1):
        text = base + "\n本次参数：" + json.dumps(params, ensure_ascii=False, sort_keys=True)
        eid = db.create_experiment(f"{prefix} #{idx}", text,
                                   template_id=(template or {}).get("id", ""),
                                   project_id=pid)
        db.update_experiment(eid, plan_json=json.dumps(
            {"parameter_grid": params, "batch_index": idx, "batch_size": len(combos)},
            ensure_ascii=False))
        db.add_audit(eid, "experiment", "批量创建",
                     {"batch_index": idx, "params": params,
                      "template_id": (template or {}).get("id", "")}, decision="已创建")
        db.add_event(eid, None, f"批量实验 #{idx} 已创建", kind="stage")
        ids.append(eid)
    return jsonify({"ok": True, "ids": ids, "count": len(ids)})


@api_bp.get("/api/batch/compare")
def api_batch_compare():
    """批量实验结果对比表：参数组合 + 各自最新指标，直观看哪组最优。"""
    raw = request.args.get("ids", "")
    ids = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    ids = ids[:50]
    if not ids:
        return jsonify({"error": "请提供要对比的实验 id"}), 400
    rows = []
    for eid in ids:
        exp = own_exp_or_404(eid, "read")
        plan = json.loads(exp.get("plan_json") or "{}")
        latest = db.latest_run(eid)
        summary = None
        if latest and latest.get("summary_json"):
            try:
                summary = json.loads(latest["summary_json"])
            except Exception:
                pass
        stats = (summary or {}).get("metrics_stats") or []
        metrics = {m.get("name"): {"last": m.get("last"), "best": m.get("best"),
                                   "step": m.get("last_step")} for m in stats}
        # 多 run 统计汇总（mean±std / n），让对比不再只看单次值
        try:
            from core import stats as stats_mod
            multi = stats_mod.experiment_stats(eid).get("metrics", {})
            for name, s in multi.items():
                metrics.setdefault(name, {})
                metrics[name].update({"mean": s.get("mean"), "std": s.get("std"),
                                      "n": s.get("n"), "ci95": s.get("ci95")})
        except Exception:
            pass
        rows.append({
            "id": eid, "title": exp["title"], "status": exp["status"],
            "template_id": exp.get("template_id") or "",
            "params": plan.get("parameter_grid") or {},
            "run_id": (latest or {}).get("id"), "run_status": (latest or {}).get("status"),
            "git_sha": (latest or {}).get("git_sha") or "", "metrics": metrics,
        })
    names = []
    for row in rows:
        for name in row["metrics"]:
            if name not in names:
                names.append(name)
    return jsonify({"rows": rows, "metric_names": names})


@api_bp.get("/api/experiments/<int:eid>/stats")
def api_exp_stats(eid):
    own_exp_or_404(eid, "read")
    aggregate = request.args.get("aggregate", "last")
    from core import stats as stats_mod
    return jsonify(stats_mod.experiment_stats(eid, aggregate=aggregate))


@api_bp.post("/api/stats/compare")
def api_stats_compare():
    d = request.get_json(force=True) or {}
    a, b = d.get("a"), d.get("b")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return jsonify({"error": "a / b 需为 {experiment_id, run_ids?} 结构"}), 400
    eid_a, eid_b = int(a.get("experiment_id")), int(b.get("experiment_id"))
    own_exp_or_404(eid_a, "read"); own_exp_or_404(eid_b, "read")
    metric = str(d.get("metric") or "").strip()
    if not metric:
        return jsonify({"error": "请指定指标名"}), 400
    runs_a = db.list_runs(eid_a); runs_b = db.list_runs(eid_b)
    if a.get("run_ids"):
        keep = {int(x) for x in a["run_ids"]}
        runs_a = [r for r in runs_a if r["id"] in keep]
    if b.get("run_ids"):
        keep = {int(x) for x in b["run_ids"]}
        runs_b = [r for r in runs_b if r["id"] in keep]
    from core import stats as stats_mod
    return jsonify(stats_mod.compare_groups(metric, runs_a, runs_b,
                                             aggregate=d.get("aggregate", "last")))


@api_bp.post("/api/batch/retry-failed")
def api_batch_retry_failed():
    """断点续跑：只重跑批量中失败/失联/被停止且已有代码的实验。"""
    d = request.get_json(force=True) or {}
    ids = d.get("ids") or []
    if not isinstance(ids, list):
        return jsonify({"error": "ids 必须是数组"}), 400
    ids = [int(x) for x in ids if str(x).strip().isdigit()][:50]
    try:
        host_id = int(d["host_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "请选择运行服务器 host_id"}), 400
    if not own_host_or_404_safe(host_id):
        return jsonify({"error": "主机不存在"}), 400

    started, skipped = [], []
    for eid in ids:
        exp = own_exp_or_404(eid, "run")
        latest = db.latest_run(eid)
        if not latest or latest["status"] not in ("failed", "lost", "stopped"):
            skipped.append({"id": eid, "reason": "无需重试"})
            continue
        if not exp.get("code_json"):
            skipped.append({"id": eid, "reason": "尚无代码，请先生成"})
            continue
        code = json.loads(exp["code_json"])
        for f in code.get("files", []):
            try:
                f["content"] = workspace.read_file(eid, f["path"])
            except Exception:
                pass
        seed = int(os.environ.get("REKIT_SEED") or random.SystemRandom().randint(1, 2147483647))
        run_id = db.create_run(eid, host_id, remote_dir="", local_dir=workspace.exp_dir(eid))
        db.update_run(run_id, env_json=json.dumps({"seed": seed}))
        try:
            sha = gitlineage.commit_snapshot(eid, code, run_id,
                                             message=f"retry from run #{latest['id']}")
        except Exception as e:
            db.update_run(run_id, status="failed", finished_at=db.now())
            skipped.append({"id": eid, "reason": crypto.redact(f"快照失败：{e}")})
            continue
        db.update_run(run_id, git_sha=sha)
        db.add_audit(eid, "run", "断点续跑",
                     {"from_run": latest["id"], "run_id": run_id, "sha": sha,
                      "host_id": host_id, "seed": seed}, decision="重试失败项", run_id=run_id)
        db.add_event(eid, run_id, f"从失败 run #{latest['id']} 续跑，快照 {sha[:8]}", kind="stage")
        db.update_experiment(eid, status="running")
        bus.status(eid, "running", run_id)
        bg(eid, "run", lambda _e, _r=run_id, _h=host_id, _c=code, _s=seed:
           _job_run(_e, _r, _h, _c, _s))
        started.append({"id": eid, "run_id": run_id, "sha": sha})
    return jsonify({"ok": True, "started": started, "skipped": skipped})


@api_bp.get("/api/experiments")
def api_list_exp():
    q = request.args.get("q", "").strip()
    tags = [t.strip() for t in request.args.get("tags", "").split(",") if t.strip()]
    status = request.args.get("status", "").strip()
    arch = request.args.get("archived", "")
    archived = None if arch == "" else (arch.lower() in ("1", "true", "yes"))
    project_id = request.args.get("project_id", type=int)
    inc_arch_project = request.args.get("include_archived_projects", "") in ("1", "true", "yes")
    return jsonify(db.list_experiments(search=q, tags=tags, status=status, archived=archived,
                                       project_id=project_id,
                                       include_archived_projects=inc_arch_project))


# ---------------- 项目 / 工作区与角色 ----------------
@api_bp.get("/api/projects")
def api_list_projects():
    include = request.args.get("archived", "") in ("1", "true", "yes")
    return jsonify(db.list_projects(include_archived=include))


@api_bp.post("/api/projects")
def api_create_project():
    d = request.get_json(force=True) or {}
    name = str(d.get("name") or "").strip()
    if not name or len(name) > 80:
        return jsonify({"error": "项目名称需为 1～80 字"}), 400
    pid = db.create_project(name, str(d.get("description") or "")[:300])
    return jsonify({"ok": True, "id": pid})


@api_bp.get("/api/projects/<int:pid>")
def api_get_project(pid):
    project = own_project_or_404(pid)
    return jsonify({
        "project": project,
        "members": db.list_project_members(pid),
        "experiments": db.list_experiments(project_id=pid, include_archived_projects=True),
        "shares": [_share_public(s) for s in db.list_project_share_links(pid)],
    })


@api_bp.post("/api/projects/<int:pid>/members")
def api_set_project_member(pid):
    own_project_or_404(pid, "manage")
    d = request.get_json(force=True) or {}
    user = db.get_user_by_name(str(d.get("username") or "").strip())
    if not user:
        return jsonify({"error": "找不到这个用户名"}), 404
    role = str(d.get("role") or "viewer").strip().lower()
    if role not in db.ROLE_RANK:
        return jsonify({"error": "角色只能是 owner / editor / viewer"}), 400
    if not db.set_project_member(pid, user["id"], role):
        return jsonify({"error": "无法更新成员角色"}), 400
    return jsonify({"ok": True, "members": db.list_project_members(pid)})


@api_bp.delete("/api/projects/<int:pid>/members/<int:target_uid>")
def api_remove_project_member(pid, target_uid):
    own_project_or_404(pid, "manage")
    if not db.remove_project_member(pid, target_uid):
        return jsonify({"error": "项目拥有者不能被移除，或成员不存在"}), 400
    return jsonify({"ok": True, "members": db.list_project_members(pid)})


@api_bp.post("/api/projects/<int:pid>/archive")
def api_archive_project(pid):
    own_project_or_404(pid, "manage")
    d = request.get_json(force=True) or {}
    archived = bool(d.get("archived"))
    if not db.archive_project(pid, archived):
        return jsonify({"error": "无法归档项目"}), 400
    return jsonify({"ok": True, "archived": archived})


@api_bp.put("/api/projects/<int:pid>")
def api_update_project(pid):
    own_project_or_404(pid, "manage")
    d = request.get_json(force=True) or {}
    name = str(d.get("name") or "").strip()
    description = str(d.get("description") or "")
    if not db.update_project(pid, name, description):
        return jsonify({"error": "项目名称需为 1～80 字"}), 400
    return jsonify({"ok": True, "project": db.get_project(pid)})


@api_bp.delete("/api/projects/<int:pid>")
def api_delete_project(pid):
    own_project_or_404(pid, "manage")
    project = db.get_project(pid)
    if project and project.get("kind") == "personal":
        return jsonify({"error": "个人工作区不能删除"}), 400
    if not db.delete_project(pid):
        return jsonify({"error": "无法删除项目"}), 400
    return jsonify({"ok": True})


@api_bp.post("/api/experiments/<int:eid>/project")
def api_move_experiment(eid):
    own_exp_or_404(eid, "delete")
    d = request.get_json(force=True) or {}
    pid = int(d.get("project_id") or 0)
    if not pid:
        pid = db.ensure_personal_project(g._auth_user["id"])
    own_project_or_404(pid, "write")
    if not db.set_experiment_project(eid, pid):
        return jsonify({"error": "无法移动实验到该项目"}), 400
    return jsonify({"ok": True, "project_id": pid})


@api_bp.post("/api/experiments/<int:eid>/tags")
def api_set_exp_tags(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    raw = d.get("tags", "")
    if isinstance(raw, list):
        tags = [str(t).strip() for t in raw if str(t).strip()][:20]
    else:
        tags = [t.strip() for t in str(raw).split(",") if t.strip()][:20]
    db.update_experiment(eid, tags=",".join(tags))
    db.add_audit(eid, "experiment", "更新标签", {"tags": tags})
    return jsonify({"ok": True, "tags": tags})


@api_bp.post("/api/experiments/<int:eid>/archive")
def api_archive_exp(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    archived = 1 if d.get("archived") else 0
    db.update_experiment(eid, archived=archived)
    db.add_audit(eid, "experiment", "归档实验" if archived else "取消归档", {})
    return jsonify({"ok": True, "archived": bool(archived)})


@api_bp.get("/api/timeline")
def api_timeline():
    limit = min(int(request.args.get("limit", 80)), 500)
    return jsonify(db.global_timeline(limit=limit))


def _copilot_gate(exp, expected):
    if exp.get("mode") != "copilot":
        return None
    checkpoint = exp.get("checkpoint") or ""
    if checkpoint and checkpoint != expected:
        return f"副驾驶模式：请先在「{checkpoint}」检查点确认后再继续"
    return None


@api_bp.post("/api/experiments/<int:eid>/mode")
def api_set_mode(eid):
    exp = own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    mode = d.get("mode")
    if mode not in ("autonomous", "copilot"):
        return jsonify({"error": "mode 只能是 autonomous 或 copilot"}), 400
    if mode == "copilot":
        checkpoint = exp.get("checkpoint") or (
            "run" if exp.get("code_json") else "code" if exp.get("plan_json") else "plan")
    else:
        checkpoint = ""
    db.update_experiment(eid, mode=mode, checkpoint=checkpoint)
    db.add_audit(eid, "experiment", "切换协作模式",
                 {"from": exp.get("mode"), "to": mode, "checkpoint": checkpoint},
                 decision="已切换")
    return jsonify({"ok": True, "mode": mode, "checkpoint": checkpoint})


@api_bp.post("/api/experiments/<int:eid>/criteria")
def api_save_criteria(eid):
    exp = own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    criteria = d.get("criteria")
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria or "{}")
        except Exception:
            return jsonify({"error": "判据 JSON 格式错误"}), 400
    if not isinstance(criteria, dict):
        return jsonify({"error": "判据必须是对象"}), 400
    reason = str(d.get("reason") or "").strip()
    old = json.loads(exp.get("criteria_json") or "{}")
    history = json.loads(exp.get("criteria_history_json") or "[]")
    changed = json.dumps(old, ensure_ascii=False, sort_keys=True) != \
        json.dumps(criteria, ensure_ascii=False, sort_keys=True)
    if exp.get("criteria_locked") and changed and not reason:
        return jsonify({"error": "判据已锁定，修改时必须说明原因"}), 400
    if changed and exp.get("criteria_locked"):
        history.append({"criteria": old, "locked_at": exp.get("criteria_locked_at"),
                        "reason": reason, "changed_at": db.now()})
    fields = {"criteria_json": json.dumps(criteria, ensure_ascii=False),
              "criteria_history_json": json.dumps(history, ensure_ascii=False)}
    if d.get("lock"):
        fields["criteria_locked"] = 1
        fields["criteria_locked_at"] = exp.get("criteria_locked_at") or db.now()
    db.update_experiment(eid, **fields)
    db.add_audit(eid, "criteria", "保存判据" if not changed else "修改已锁定判据",
                 {"before": old, "after": criteria, "reason": reason,
                  "locked": bool(d.get("lock") or exp.get("criteria_locked"))},
                 decision="已锁定" if d.get("lock") else "已保存")
    return jsonify({"ok": True, "criteria": criteria,
                    "locked": bool(fields.get("criteria_locked", exp.get("criteria_locked")))})


@api_bp.post("/api/experiments/<int:eid>/innovation")
def api_save_innovation(eid):
    """换一份文稿：把新抽取的创新点写回实验并记录审计。"""
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    text = str(d.get("innovation") or "").strip()
    if len(text) < 10:
        return jsonify({"error": "创新点描述太短，至少写 10 个字"}), 400
    exp = db.get_experiment(eid)
    db.update_experiment(eid, innovation=text, title=str(d.get("title") or exp["title"]).strip())
    db.add_audit(eid, "experiment", "更换创新点文稿",
                 {"before": exp.get("innovation", "")[:240], "after": text[:240]},
                 decision="已更新")
    db.add_event(eid, None, "已更换创新点文稿，可重新生成方案", kind="stage")
    return jsonify({"ok": True, "innovation": text})


@api_bp.post("/api/experiments/<int:eid>/review")
def api_run_review(eid):
    own_exp_or_404(eid, "write")
    from core import review
    latest = db.latest_run(eid)
    if not latest:
        return jsonify({"error": "还没有实验结果可审阅"}), 400
    summary = json.loads(latest.get("summary_json") or "{}")
    if not summary:
        return jsonify({"error": "请先生成实验结论"}), 400
    result = review.review_summary(eid, latest["id"], summary)
    if result.get("error"):
        return jsonify(result), 200
    return jsonify({"ok": True, "review": result})


@api_bp.get("/api/runs/<int:rid>/diagnosis")
def api_run_diagnosis(rid):
    run = db.q1("SELECT * FROM runs WHERE id=?", (rid,))
    if not run:
        abort(404)
    own_exp_or_404(run["experiment_id"], "read")
    cached = run.get("diagnosis_json")
    if cached:
        return jsonify(json.loads(cached))
    from core import diagnose
    dg = diagnose.diagnose_run(rid)
    return jsonify(dg or {})


@api_bp.post("/api/runs/<int:rid>/diagnosis")
def api_run_rediagnose(rid):
    run = db.q1("SELECT * FROM runs WHERE id=?", (rid,))
    if not run:
        abort(404)
    own_exp_or_404(run["experiment_id"], "write")
    from core import diagnose
    dg = diagnose.diagnose_run(rid) or {}
    db.update_run(rid, diagnosis_json=json.dumps(dg, ensure_ascii=False))
    return jsonify({"ok": True, "diagnosis": dg})


@api_bp.get("/api/experiments/<int:eid>/audits")
def api_audits(eid):
    own_exp_or_404(eid, "read")
    return jsonify(db.list_audits(eid, 200))


@api_bp.get("/api/experiments/<int:eid>/usage")
def api_exp_usage(eid):
    own_exp_or_404(eid, "read")
    return jsonify(db.usage_summary(eid))


@api_bp.get("/api/usage")
def api_usage():
    return jsonify(db.usage_summary())


@api_bp.get("/api/experiments/<int:eid>")
def api_get_exp(eid):
    exp = own_exp_or_404(eid, "read")
    out = dict(exp)
    out["plan"] = json.loads(exp.get("plan_json") or "null")
    out["code"] = json.loads(exp.get("code_json") or "null")
    out["runs"] = db.list_runs(eid)
    latest = db.latest_run(eid)
    out["latest_run"] = latest
    out["summary"] = json.loads(latest["summary_json"]) if (latest and latest.get("summary_json")) else None
    out["files"] = workspace.list_files(eid)
    out["criteria"] = json.loads(exp.get("criteria_json") or "{}")
    out["criteria_history"] = json.loads(exp.get("criteria_history_json") or "[]")
    out["audits"] = db.list_audits(eid, 100)
    out["ai_outputs"] = db.list_ai_outputs(experiment_id=eid, uid=g._auth_user["id"], limit=80)
    out["usage"] = db.usage_summary(eid)
    # 最近事件，供命令行判断任务是否失败
    evs = db.events_after(eid, 0, 500)[-25:]
    out["events"] = [{"kind": e["kind"], "level": e["level"],
                      "message": e["message"], "ts": e["ts"]} for e in evs]
    return jsonify(out)


@api_bp.delete("/api/experiments/<int:eid>")
def api_del_exp(eid):
    own_exp_or_404(eid, "delete")
    workspace.cleanup(eid)
    db.delete_experiment(eid)
    return jsonify({"ok": True})


@api_bp.post("/api/experiments/<int:eid>/plan")
def api_plan(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) if request.data else {}
    bg(eid, "plan", _job_plan, d.get("feedback"))
    return jsonify({"ok": True, "job": "plan"})


@api_bp.post("/api/experiments/<int:eid>/code")
def api_code(eid):
    exp = own_exp_or_404(eid, "write")
    blocked = _copilot_gate(exp, "code")
    if blocked:
        return jsonify({"error": blocked}), 409
    d = request.get_json(force=True) if request.data else {}
    host_id = d.get("host_id")
    if not host_id:
        last = db.latest_run(eid)
        host_id = last["host_id"] if last else None
    if exp.get("mode") == "copilot" and exp.get("checkpoint") == "code":
        db.update_experiment(eid, checkpoint="")
    bg(eid, "code", _job_code, d.get("feedback"), host_id)
    return jsonify({"ok": True, "job": "code"})


@api_bp.post("/api/experiments/<int:eid>/run")
def api_run(eid):
    exp = own_exp_or_404(eid, "run")
    blocked = _copilot_gate(exp, "run")
    if blocked:
        return jsonify({"error": blocked}), 409
    d = request.get_json(force=True)
    host_id = int(d["host_id"])
    if not exp.get("code_json"):
        return jsonify({"error": "还没有代码，先执行「生成代码」"}), 400
    if exp.get("mode") == "copilot" and exp.get("checkpoint") == "run":
        db.update_experiment(eid, checkpoint="")
    if not own_host_or_404_safe(host_id):
        return jsonify({"error": "主机不存在"}), 400
    code = json.loads(exp["code_json"])
    # 把本地可能被手动改过的文件同步进 code 结构
    for f in code.get("files", []):
        try:
            f["content"] = workspace.read_file(eid, f["path"])
        except Exception:
            pass
    run_id = db.create_run(eid, host_id,
                           remote_dir="", local_dir=workspace.exp_dir(eid))
    seed = int(os.environ.get("REKIT_SEED") or random.SystemRandom().randint(1, 2147483647))
    db.update_run(run_id, env_json=json.dumps({"seed": seed}))
    try:
        sha = gitlineage.commit_snapshot(eid, code, run_id,
                                         message=exp["title"])
    except Exception as e:
        msg = crypto.redact(f"git 快照创建失败：{e}")
        db.update_run(run_id, status="failed", finished_at=db.now())
        db.add_event(eid, run_id, msg, kind="error", level="error")
        return jsonify({"error": msg}), 500
    db.update_run(run_id, git_sha=sha)
    db.add_audit(eid, "run", "锁定代码快照",
                 {"run_id": run_id, "sha": sha, "host_id": host_id,
                  "files": [f.get("path") for f in code.get("files", [])]},
                 decision="开始运行", run_id=run_id)
    db.add_event(eid, run_id, f"代码快照已锁定：{sha[:8]}", kind="stage")
    db.update_experiment(eid, status="running")
    bus.status(eid, "running", run_id)
    bg(eid, "run", lambda _eid, _rid=run_id, _hid=host_id, _code=code, _seed=seed:
       _job_run(_eid, _rid, _hid, _code, _seed))
    return jsonify({"ok": True, "run_id": run_id, "git_sha": sha, "seed": seed})


@api_bp.get("/api/experiments/<int:eid>/files")
def api_files(eid):
    own_exp_or_404(eid, "read")
    return jsonify(workspace.list_files(eid))


@api_bp.get("/api/experiments/<int:eid>/file")
def api_read_file(eid):
    own_exp_or_404(eid, "read")
    p = request.args.get("path", "")
    return jsonify({"path": p, "content": workspace.read_file(eid, p)})


@api_bp.post("/api/experiments/<int:eid>/file")
def api_save_file(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True)
    path = d["path"]
    before = workspace.read_file(eid, path)
    after = str(d.get("content") or "")
    workspace.save_file(eid, path, after)
    db.add_audit(eid, "code", "修改代码文件",
                 {"path": path, "before": before[:300], "after": after[:300],
                  "bytes_before": len(before), "bytes_after": len(after)},
                 decision="已保存")
    return jsonify({"ok": True})


# ---------------- 运行 / 指标 / 总结 ----------------

@api_bp.get("/api/experiments/<int:eid>/metrics")
def api_metrics(eid):
    own_exp_or_404(eid, "read")
    run_id = request.args.get("run_id", type=int)
    if not run_id:
        r = db.latest_run(eid)
        run_id = r["id"] if r else None
    if not run_id:
        return jsonify({"run_id": None, "series": {}})
    # 防越权：指定的 run 必须属于本实验（也就属于当前用户）
    if not any(x["id"] == run_id for x in db.list_runs(eid)):
        abort(404)
    return jsonify({"run_id": run_id, "series": db.metrics_series(run_id)})


@api_bp.get("/api/experiments/<int:eid>/runs")
def api_runs(eid):
    own_exp_or_404(eid, "read")
    return jsonify(db.list_runs(eid))


@api_bp.get("/api/runs/<int:rid>/diff")
def api_run_diff(rid):
    """查看某次运行对应的代码快照相对上一次运行的 diff。"""
    run = own_run_or_404(rid)
    if not run.get("git_sha"):
        return jsonify({"error": "这次运行没有锁定 git 代码快照"}), 400
    base_sha = ""
    base_run_id = request.args.get("base_run_id", type=int)
    if base_run_id:
        base_run = own_run_or_404(base_run_id)
        if base_run["experiment_id"] != run["experiment_id"]:
            return jsonify({"error": "对比的运行不属于同一实验"}), 400
        base_sha = base_run.get("git_sha") or ""
    else:
        for item in db.list_runs(run["experiment_id"]):
            if item["id"] < rid and item.get("git_sha"):
                base_sha = item["git_sha"]
                break
    try:
        patch = gitlineage.diff(run["experiment_id"], base_sha, run["git_sha"])
    except Exception as e:
        return jsonify({"error": crypto.redact(str(e))}), 500
    return jsonify({"base": base_sha, "target": run["git_sha"],
                    "patch": patch or "无代码变更"})


@api_bp.post("/api/runs/<int:rid>/restore")
def api_run_restore(rid):
    """把某次运行的代码快照恢复到当前实验工作区。"""
    run = own_run_or_404(rid, "write")
    if not run.get("git_sha"):
        return jsonify({"error": "这次运行没有锁定 git 代码快照"}), 400
    eid = run["experiment_id"]
    try:
        code = gitlineage.code_at(eid, run["git_sha"])
        gitlineage.restore(eid, run["git_sha"])
        workspace.write_files(eid, code)
        new_sha = gitlineage.commit_snapshot(eid, code, None,
                                             message=f"restore from run #{rid}")
        db.update_experiment(eid, code_json=json.dumps(code, ensure_ascii=False),
                             status="coded", updated_at=db.now())
        db.add_event(eid, rid, f"已从 run #{rid} 恢复代码快照", kind="stage")
    except Exception as e:
        return jsonify({"error": crypto.redact(str(e))}), 500
    return jsonify({"ok": True, "sha": new_sha, "files": len(code.get("files", []))})


@api_bp.post("/api/runs/<int:rid>/fork")
def api_run_fork(rid):
    """从某次运行的代码快照派生一个新实验，保留 git 谱系。"""
    run = own_run_or_404(rid)
    if not run.get("git_sha"):
        return jsonify({"error": "这次运行没有锁定 git 代码快照"}), 400
    eid = run["experiment_id"]
    exp = db.get_experiment(eid)
    d = request.get_json(silent=True) or {}
    title = str(d.get("title") or "").strip() or f"{exp['title']} · fork"
    new_eid = db.create_experiment(title, exp["innovation"])
    try:
        gitlineage.clone_for_fork(eid, new_eid, run["git_sha"], rid)
        code = gitlineage.code_at(new_eid, run["git_sha"])
        db.update_experiment(
            new_eid, plan_json=exp.get("plan_json") or "",
            code_json=json.dumps(code, ensure_ascii=False), status="coded",
            forked_from_experiment_id=eid, forked_from_run_id=rid,
            updated_at=db.now())
        db.add_event(new_eid, None, f"从实验 #{eid} 的 run #{rid} 派生", kind="stage")
    except Exception as e:
        workspace.cleanup(new_eid)
        db.delete_experiment(new_eid)
        return jsonify({"error": crypto.redact(str(e))}), 500
    return jsonify({"ok": True, "id": new_eid, "title": title})


@api_bp.post("/api/runs/<int:rid>/summarize")
def api_summarize(rid):
    run = own_run_or_404(rid, "write")
    try:
        s = summarizer.summarize(rid, run["experiment_id"], force=True)
        return jsonify(s)
    except Exception as e:
        return jsonify({"error": crypto.redact(str(e))}), 500


@api_bp.post("/api/runs/<int:rid>/stop")
def api_stop(rid):
    run = own_run_or_404(rid, "run")
    host = db.get_host(run["host_id"], include_secrets=True)
    if not host:
        abort(404)
    try:
        client = ssh_runner.connect(host)
        ssh_runner.run(client, f"cd {run['remote_dir']} && [ -f .run.pid ] && kill $(cat .run.pid) || true")
        client.close()
        db.update_run(rid, status="stopped", finished_at=db.now())
        bus.note(run["experiment_id"], "已发送停止信号", "warn", rid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": crypto.redact(str(e))}), 500


# ---------------- 报告导出 ----------------

def _report_or_404(eid):
    data = report.build(eid)
    if data is None:
        abort(404)
    return data


@api_bp.get("/api/experiments/<int:eid>/report.md")
def api_report_md(eid):
    own_exp_or_404(eid, "read")
    data = _report_or_404(eid)
    body = report.markdown(data)
    resp = Response(body, mimetype="text/markdown; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''report_exp{eid}.md")
    return resp


@api_bp.get("/api/experiments/<int:eid>/report.docx")
def api_report_docx(eid):
    own_exp_or_404(eid, "read")
    data = _report_or_404(eid)
    try:
        content = report.docx_bytes(data)
    except ImportError:
        return jsonify({"error": "服务器未安装 python-docx，无法导出 Word；请导出 Markdown 或打印 PDF"}), 501
    resp = Response(content, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    resp.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''report_exp{eid}.docx")
    return resp


@api_bp.get("/api/experiments/<int:eid>/report/print")
def api_report_print_data(eid):
    """打印页用的结构化数据（含核验/审计/复现配置）。"""
    own_exp_or_404(eid, "read")
    return jsonify(_report_or_404(eid))


# ---------------- SSE ----------------

@api_bp.get("/api/experiments/<int:eid>/stream")
def api_stream(eid):
    own_exp_or_404(eid, "read")
    # 浏览器重连会自动带 Last-Event-ID；从事件日志补发，重启也不丢
    last_id = request.headers.get("Last-Event-ID", "").strip()
    last_id = int(last_id) if last_id.isdigit() else 0

    def gen():
        nonlocal last_id
        q = bus.subscribe(eid)
        try:
            yield "retry: 3000\n\n"
            last_ping = time.time()
            # 1) 先补发游标之后的落库事件；没有游标时从最新开始（避免灌满历史）
            if last_id:
                for row in db.stream_events_after(eid, last_id):
                    try:
                        payload = json.loads(row["payload"])
                    except Exception:
                        continue
                    payload["_id"] = row["id"]
                    last_id = max(last_id, row["id"])
                    yield f"id: {row['id']}\n" + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            else:
                last_id = db.stream_event_cursor(eid)

            while True:
                try:
                    item = q.get(timeout=1.0)
                    eid_val = item.get("_id") or 0
                    # 去重：落库补发过的事件不要再发一次
                    if eid_val and eid_val <= last_id:
                        continue
                    if eid_val:
                        last_id = eid_val
                    yield (f"id: {eid_val}\n" if eid_val else "") + \
                          "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        yield ": ping\n\n"
                        last_ping = time.time()
        except GeneratorExit:
            pass
        finally:
            bus.unsubscribe(eid, q)
            try:
                db.prune_stream_events(eid, keep=2000)
            except Exception:
                pass
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ---------------- 网页终端（WebSocket） ----------------

@api_bp.get("/api/terminal/hosts")
def api_terminal_hosts():
    rows = []
    for h in db.list_hosts():
        rows.append({"id": h["id"], "name": h["name"], "host": h["host"],
                     "port": h["port"], "username": h["username"],
                     "gpu": h["gpu"]})
    return jsonify(rows)


def _origin_allowed():
    origin = request.headers.get("Origin") or request.headers.get("Sec-WebSocket-Origin")
    if not origin:
        return True
    o = urlparse(origin)
    allowed = {request.host.lower()}
    for item in (_BOOT_CFG["security"].get("allowed_origins") or []):
        allowed.add(str(item).lower())
    if o.netloc.lower() not in allowed:
        return False
    if _SCHEME == "https" and o.scheme not in ("https", "wss"):
        return False
    return True


@sock.route("/ws/terminal/<int:hid>")
def ws_terminal(ws, hid):
    """单线程轮转：wsproto 不是线程安全的，收发必须串行。"""
    def _ws_error(msg):
        try:
            ws.send(json.dumps({"type": "error", "message": msg}, ensure_ascii=False))
        except Exception:
            pass

    if not _origin_allowed():
        _ws_error("Origin 校验失败，拒绝建立终端连接")
        return
    u = auth.current_user()
    if not u:
        _ws_error("未登录，无法打开终端")
        return
    config.set_current_uid(u["id"])
    if not db.get_host(hid):
        _ws_error("服务器不存在或无权限")
        return

    ticket = db.consume_terminal_ticket(request.args.get("ticket"), u["id"], hid)
    if not ticket:
        if not os.environ.get("REKIT_ALLOW_LEGACY_WS"):
            _ws_error("终端票据无效或已使用，请重新打开终端")
            return
        ticket = {"read_only": False}
    read_only = bool(ticket.get("read_only"))

    cols = request.args.get("cols", type=int) or 120
    rows = request.args.get("rows", type=int) or 32
    term_cfg = _BOOT_CFG["terminal"]
    idle_timeout = max(30, int(term_cfg.get("idle_timeout_sec", 900)))
    max_seconds = max(60, int(term_cfg.get("max_seconds", 14400)))
    last_seen = started_at = time.time()
    print(f"[ws-term] connect host={hid} user={u['username']} {cols}x{rows} read_only={read_only}", flush=True)
    try:
        sess = terminal.TerminalSession(hid, cols, rows)
    except Exception as e:
        print(f"[ws-term] session failed: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            ws.send(json.dumps({"type": "error", "message": crypto.redact(str(e))}, ensure_ascii=False))
        except Exception:
            pass
        return

    reason = "unknown"
    try:
        ws.send(json.dumps({"type": "ready", "host": sess.host["name"],
                            "cols": sess.cols, "rows": sess.rows,
                            "read_only": read_only}, ensure_ascii=False))
    except Exception as e:
        print(f"[ws-term] ready send failed: {type(e).__name__}: {e}", flush=True)
        sess.close()
        return

    try:
        while True:
            # simple-websocket 收到对端关闭后会把 connected 置 False
            if not getattr(ws, "connected", True):
                reason = "client closed"
                break

            now = time.time()
            if now - last_seen > idle_timeout or now - started_at > max_seconds:
                reason = "session timeout"
                _ws_error("终端因超时已自动断开")
                break

            data, ended = sess.read_nowait()
            if data:
                try:
                    ws.send(data)
                except Exception as e:
                    reason = f"send failed: {type(e).__name__}: {e}"
                    break
            if ended:
                reason = "remote shell ended"
                try:
                    ws.send(json.dumps({"type": "exit"}))
                except Exception:
                    pass
                break

            # 有输出积压时不等待前端输入，先把屏幕刷出去
            timeout = 0 if sess.has_pending() else 0.02
            try:
                # 注意：simple-websocket 1.x 超时返回 None，不是抛 TimeoutError
                msg = ws.receive(timeout=timeout)
            except TimeoutError:
                continue
            except Exception as e:
                # 对端正常挂断也会走到这里，别当成异常
                if type(e).__name__ == "ConnectionClosed":
                    reason = "client closed"
                else:
                    reason = f"receive failed: {type(e).__name__}: {e}"
                break
            if msg is None:
                continue

            last_seen = time.time()
            if isinstance(msg, bytes):
                if not read_only:
                    sess.send(msg)
                continue

            if msg.startswith("{"):
                try:
                    obj = json.loads(msg)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    t = obj.get("type")
                    if t == "input" and not read_only:
                        sess.send(obj.get("data") or "")
                    elif t == "resize":
                        sess.resize(obj.get("cols"), obj.get("rows"))
                    elif t == "ping":
                        try:
                            ws.send(json.dumps({"type": "pong"}))
                        except Exception:
                            pass
                    continue
            if not read_only:
                sess.send(msg)
    except Exception as e:
        reason = f"loop crashed: {type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        print(f"[ws-term] closed host={hid} ({reason})", flush=True)
        sess.close()


# ---------------- 文稿读取 ----------------

@api_bp.post("/api/extract")
def api_extract():
    """把用户拖进来的文稿抽成纯文本（创新点、参考资料都走这里）。"""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "没有收到文件"}), 400
    try:
        text = extract.extract_bytes(f.filename, f.read())
    except extract.ExtractError as e:
        return jsonify({"error": crypto.redact(str(e))}), 400
    return jsonify({"text": text, "name": f.filename, "chars": len(text)})


# ---------------- 主机 ----------------

@api_bp.get("/api/hosts")
def api_hosts():
    return jsonify(db.list_hosts())


_PROCESS_CACHE = {}
_PROCESS_CACHE_TTL = 15
_PROCESS_CACHE_LOCK = threading.Lock()
_PY_CMD_RE = re.compile(r"(^|[\s/])python([0-9.]*)($|\s)", re.I)
_PY_SCRIPT_RE = re.compile(r"""(?:^|\s)(['"]?)([^\s'"]+\.py)\1""")
_LIBRARY_PATH_HINTS = (
    "site-packages/", "dist-packages/", "torch/_inductor/", "compile_worker",
    "multiprocessing.spawn", "multiprocessing.resource_tracker",
    "/init/jupyter/", "jupyter_config.py", "ipykernel_launcher",
)


def _extract_script(cmd):
    for m in _PY_SCRIPT_RE.finditer(cmd):
        token = m.group(2)
        if token.startswith("-") or "=" in token:
            continue
        return token
    m = re.search(r"-m\s+([^\s]+)", cmd, re.I)
    if m:
        return "module:" + m.group(1)
    return cmd.split()[0] if cmd.split() else "python"


def _scan_python_processes(host):
    """SSH 到目标机器扫 Python 进程；结果短缓存，避免页面轮询频繁连 SSH。"""
    key = (host["id"], host["host"], host["port"], host["username"])
    now_ts = db.now()
    with _PROCESS_CACHE_LOCK:
        cached = _PROCESS_CACHE.get(key)
        if cached and now_ts - cached["ts"] < _PROCESS_CACHE_TTL:
            return cached["items"]

    client = ssh_runner.connect(host, timeout=8)
    try:
        code, out, err = ssh_runner.run(
            client, "ps -eo pid,ppid,etimes,pcpu,pmem --no-headers", timeout=12)
        if code != 0:
            raise RuntimeError((err or "远程 ps 执行失败").strip())

        metas, pids = [], []
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            pid, ppid, etimes, cpu, mem = parts[:5]
            metas.append((pid, ppid, etimes, cpu, mem))
            pids.append(pid)

        # ps 的 args 在非 UTF-8 locale 下会把中文路径变成 "?"；
        # /proc/<pid>/cmdline 是原始字节，能正确保留中文路径。
        cmds = {}
        if pids:
            cmdline_cmd = ("for p in " + " ".join(pids) +
                           "; do printf '%s\\t' \"$p\"; "
                           "tr '\\0' ' ' < /proc/$p/cmdline 2>/dev/null || true; echo; done")
            code2, out2, _ = ssh_runner.run(client, cmdline_cmd, timeout=12)
            if code2 == 0:
                for ln in out2.splitlines():
                    if "\t" in ln:
                        pid, cmd = ln.split("\t", 1)
                        cmds[pid.strip()] = cmd.strip()
            if not cmds:
                code3, out3, _ = ssh_runner.run(
                    client, "LANG=C.UTF-8 LC_ALL=C.UTF-8 ps -eo pid,args --no-headers",
                    timeout=12)
                if code3 == 0:
                    for ln in out3.splitlines():
                        parts = ln.strip().split(None, 1)
                        if len(parts) == 2 and parts[0].isdigit():
                            cmds[parts[0]] = parts[1]
    finally:
        client.close()

    items = []
    for pid, ppid, etimes, cpu, mem in metas:
        cmd = cmds.get(pid, "")
        if not cmd:
            continue
        low = cmd.lower()
        if not _PY_CMD_RE.search(cmd):
            continue
        if ".py" not in cmd and " -m " not in low:
            continue
        if any(x in low for x in ("ps -eo", "grep ", "bash -lc", "sshd:")):
            continue
        if any(x in low for x in _LIBRARY_PATH_HINTS):
            continue
        script = _extract_script(cmd)
        try:
            cpu_s = f"{float(cpu):.1f}%"
            mem_s = f"{float(mem):.1f}%"
        except ValueError:
            cpu_s = mem_s = "—"
        items.append((float(etimes or 0), {
            "pid": pid,
            "ppid": ppid,
            "script": script[:200],
            "command": cmd[:300],
            "elapsed": _elapsed_text(now_ts - float(etimes or 0), now_ts),
            "cpu": cpu_s,
            "mem": mem_s,
            "progress": {"percent": None, "text": "手动脚本，进度未知",
                         "source": "进程扫描"},
        }))
    items.sort(key=lambda x: x[0])
    items = [item for _, item in items][:20]

    with _PROCESS_CACHE_LOCK:
        _PROCESS_CACHE[key] = {"ts": db.now(), "items": items}
    return items


@api_bp.get("/api/hosts/activity")
def api_hosts_activity():
    """服务器页轮询用：按 host 返回当前运行脚本与进度。"""
    return jsonify({"hosts": _host_activity_by_host(), "server_now": db.now()})


@api_bp.get("/api/hosts/<int:hid>/processes")
def api_host_processes(hid):
    """扫描目标服务器上手动运行的 Python 脚本，不写入数据库。"""
    own_host_or_404(hid)
    host = db.get_host(hid, include_secrets=True)
    try:
        return jsonify({"ok": True, "processes": _scan_python_processes(host),
                        "scanned_at": db.now()})
    except Exception as e:
        return jsonify({"ok": False, "error": crypto.redact(str(e)), "processes": []}), 200


@api_bp.post("/api/terminal/<int:hid>/ticket")
def api_terminal_ticket(hid):
    """每次连接前签发一次性终端票据；只读模式会限制输入。"""
    own_host_or_404(hid)
    u = auth.current_user()
    body = request.get_json(silent=True) or {}
    ttl = int(config.load_config(u["id"])["terminal"].get("ticket_ttl_sec", 60))
    read_only = bool(body.get("read_only"))
    ticket, expires_at = db.issue_terminal_ticket(u["id"], hid, ttl, read_only)
    return jsonify({"ticket": ticket, "expires_at": expires_at, "read_only": read_only})


@api_bp.post("/api/hosts")
def api_add_host():
    d = request.get_json(force=True)
    for k in ("name", "host", "username"):
        if not (d.get(k) or "").strip():
            return jsonify({"error": f"{k} 不能为空"}), 400
    if d.get("auth_type") == "key" and not d.get("key_path"):
        return jsonify({"error": "使用密钥登录需要填写私钥路径"}), 400
    if d.get("auth_type") != "key" and not d.get("password"):
        return jsonify({"error": "使用密码登录需要填写密码"}), 400
    hid = db.add_host(d)
    return jsonify({"id": hid})


@api_bp.delete("/api/hosts/<int:hid>")
def api_del_host(hid):
    own_host_or_404(hid)
    db.delete_host(hid)
    return jsonify({"ok": True})


@api_bp.post("/api/hosts/<int:hid>/test")
def api_test_host(hid):
    own_host_or_404(hid)
    host = db.get_host(hid, include_secrets=True)
    try:
        info = ssh_runner.test_connection(host)
        return jsonify({"ok": True, "info": info})
    except Exception as e:
        return jsonify({"ok": False, "error": crypto.redact(str(e))}), 200


# ---------------- GitHub 集成 ----------------
def _github_public(row):
    """脱敏后的配置，Token 明文永不返回前端。"""
    return {
        "id": row["id"],
        "repo_full_name": row["repo_full_name"],
        "token_hint": row["token_hint"] or "",
        "default_branch": row["default_branch"] or "main",
        "provider": row.get("provider") or "github",
    }


@api_bp.get("/api/github/configs")
def api_github_configs():
    return jsonify([_github_public(r) for r in db.list_git_remotes()])


@api_bp.post("/api/github/configs")
def api_github_save_config():
    d = request.get_json(force=True) or {}
    repo = str(d.get("repo_full_name") or "").strip()
    token = str(d.get("token") or "").strip()
    branch = str(d.get("default_branch") or "main").strip() or "main"
    edit_id = d.get("id")
    try:
        gitlineage.validate_repo_full_name(repo)
        gitlineage.validate_branch(branch)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    existing = db.get_git_remote(edit_id) if edit_id else None
    if edit_id and not existing:
        return jsonify({"error": "配置不存在"}), 404

    # 留空 token 时复用原值（前端只拿到密文时同样适用）
    if not token and existing:
        token = crypto.decrypt(existing["token_enc"])
    if not token:
        return jsonify({"error": "请填写 GitHub Token"}), 400

    try:
        login, repo = github.check_repo_access(token, repo)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    enc = crypto.encrypt(token)
    hint = token[-4:]
    if existing:
        db.update_git_remote(edit_id, repo_full_name=repo, token_enc=enc,
                             token_hint=hint, default_branch=branch)
        rid = edit_id
    else:
        rid = db.add_git_remote(repo, enc, hint, default_branch=branch)
    return jsonify({"ok": True, "config": _github_public(db.get_git_remote(rid)),
                    "login": login})


@api_bp.post("/api/github/test")
def api_github_test():
    d = request.get_json(force=True) or {}
    repo = str(d.get("repo_full_name") or "").strip()
    token = str(d.get("token") or "").strip()
    if not token and d.get("id"):
        row = db.get_git_remote(d.get("id"))
        token = crypto.decrypt(row["token_enc"]) if row else ""
    try:
        login, repo = github.check_repo_access(token, repo)
        return jsonify({"ok": True, "login": login, "repo": repo})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 200


@api_bp.delete("/api/github/configs/<int:rid>")
def api_github_delete_config(rid):
    if not db.get_git_remote(rid):
        return jsonify({"error": "配置不存在"}), 404
    db.delete_git_remote(rid)
    return jsonify({"ok": True})


@api_bp.post("/api/experiments/<int:eid>/push")
def api_experiment_push(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    rid = d.get("config_id")
    repo = str(d.get("repo_full_name") or "").strip()
    branch = str(d.get("default_branch") or "main").strip() or "main"
    token = str(d.get("token") or "").strip()

    row = db.get_git_remote(rid) if rid else None
    if not row and repo:
        row = db.get_git_remote_by_repo(repo)
    if not row:
        return jsonify({"error": "请先在设置页配置 GitHub 仓库"}), 400

    repo = row["repo_full_name"]
    branch = branch or row["default_branch"] or "main"
    if token:
        plain = token
    else:
        plain = crypto.decrypt(row["token_enc"])
    if not plain:
        return jsonify({"error": "Token 解密失败，请重新配置"}), 400

    runs = db.list_runs(eid)
    run = next((r for r in runs if str(r.get("id")) == str(d.get("run_id"))), None) if d.get("run_id") else None
    run = run or next((r for r in runs if r.get("git_sha")), None)
    if not run or not run.get("git_sha"):
        return jsonify({"error": "该实验还没有代码快照，请先运行一次实验"}), 400

    warnings = gitlineage.scan_workspace(eid)
    if warnings and not d.get("ack_sensitive"):
        return jsonify({"error": "代码中疑似包含敏感信息，请确认后再推送",
                        "warnings": warnings, "need_ack": True}), 200

    try:
        gitlineage.validate_branch(branch)
        sha = gitlineage.push_snapshot(eid, repo, plain, branch)
    except (RuntimeError, ValueError) as e:
        db.add_audit(eid, "git", "push_failed",
                     {"repo": repo, "branch": branch, "error": str(e)})
        return jsonify({"error": str(e)}), 500

    url = gitlineage.remote_commit_url(repo, sha)
    db.update_run(run["id"], remote_url=url, remote_sha=sha)
    db.add_audit(eid, "git", "push",
                 {"repo": repo, "branch": branch, "sha": sha, "url": url},
                 decision="已推送到 GitHub", run_id=run["id"])
    bus.note(eid, f"代码快照已推送到 GitHub：{repo}", "info", run["id"])
    return jsonify({"ok": True, "sha": sha, "url": url, "repo": repo,
                    "warnings": warnings})


# ---------------- 分享链接增强 ----------------
def _share_public(row):
    return {"id": row["id"], "token": row["token"],
            "url": "/observe/" + row["token"],
            "kind": "project" if row.get("project_id") else "experiment",
            "project_id": row.get("project_id") or 0,
            "has_password": bool(row["password_hash"]),
            "expires_at": row["expires_at"], "max_views": row["max_views"],
            "views": row["views"], "redact": bool(row["redact"]),
            "revoked": bool(row["revoked"]), "created_at": row["created_at"]}


def _share_params(d):
    try:
        ttl_hours = float(d.get("ttl_hours")) if d.get("ttl_hours") not in (None, "") else 168
        if ttl_hours <= 0 or ttl_hours > 24 * 365:
            raise ValueError("有效期需在 0～8760 小时之间")
    except (TypeError, ValueError):
        raise ValueError("有效期需在 0～8760 小时之间")
    try:
        max_views = int(d.get("max_views") or 0)
        if max_views < 0 or max_views > 100000:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("访问次数无效")
    password = str(d.get("password") or "")
    salt = secrets.token_hex(8) if password else ""
    return {
        "password_hash": auth.hash_share_password(password, salt) if password else "",
        "salt": salt,
        "expires_at": time.time() + ttl_hours * 3600,
        "max_views": max_views,
        "redact": 1 if d.get("redact") else 0,
        "ttl_hours": ttl_hours,
        "password": bool(password),
    }


@api_bp.get("/api/experiments/<int:eid>/shares")
def api_list_shares(eid):
    own_exp_or_404(eid, "share")
    return jsonify([_share_public(s) for s in db.list_share_links(eid)])


@api_bp.post("/api/experiments/<int:eid>/shares")
def api_create_share(eid):
    own_exp_or_404(eid, "share")
    d = request.get_json(force=True) or {}
    try:
        p = _share_params(d)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    token = auth.make_share_token()
    db.add_share_link(token, eid, password_hash=p["password_hash"], salt=p["salt"],
                      expires_at=p["expires_at"], max_views=p["max_views"],
                      redact=p["redact"])
    db.add_audit(eid, "share", "创建分享链接",
                 {"ttl_hours": p["ttl_hours"], "password": p["password"],
                  "max_views": p["max_views"], "redact": bool(p["redact"])})
    return jsonify({"ok": True, "share": _share_public(db.get_share_link(token))})


@api_bp.get("/api/projects/<int:pid>/shares")
def api_list_project_shares(pid):
    own_project_or_404(pid, "share")
    return jsonify([_share_public(s) for s in db.list_project_share_links(pid)])


@api_bp.post("/api/projects/<int:pid>/shares")
def api_create_project_share(pid):
    own_project_or_404(pid, "share")
    d = request.get_json(force=True) or {}
    try:
        p = _share_params(d)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    token = auth.make_share_token()
    db.add_share_link(token, 0, project_id=pid, password_hash=p["password_hash"],
                      salt=p["salt"], expires_at=p["expires_at"],
                      max_views=p["max_views"], redact=p["redact"])
    return jsonify({"ok": True, "share": _share_public(db.get_share_link(token))})


@api_bp.delete("/api/shares/<int:sid>")
def api_revoke_share(sid):
    row = db.q1("SELECT * FROM share_links WHERE id=?", (sid,))
    if not row:
        return jsonify({"error": "分享链接不存在"}), 404
    if row.get("project_id"):
        own_project_or_404(row["project_id"], "share")
        db.revoke_share_link(sid)
        return jsonify({"ok": True})
    own_exp_or_404(row["experiment_id"], "share")
    db.revoke_share_link(sid)
    db.add_audit(row["experiment_id"], "share", "撤销分享链接", {"id": sid})
    return jsonify({"ok": True})


@api_bp.post("/api/share/<token>/comment")
def api_share_comment(token):
    """访客批注：凭分享 token 写入，无需登录。"""
    share = db.get_share_link(token)
    if not share or share["revoked"]:
        return jsonify({"error": "链接无效"}), 404
    if share.get("project_id"):
        return jsonify({"error": "项目视图暂不支持访客批注，请使用单实验分享链接"}), 400
    if share["expires_at"] and share["expires_at"] < time.time():
        return jsonify({"error": "链接已过期"}), 410
    if share["max_views"] and share["views"] >= share["max_views"]:
        return jsonify({"error": "访问次数已用完"}), 410
    if not auth.verify_share_password(request.args.get("p", ""),
                                       share["salt"], share["password_hash"]):
        return jsonify({"error": "访问密码错误"}), 403
    d = request.get_json(force=True) or {}
    content = str(d.get("content") or "").strip()
    if not content or len(content) > 2000:
        return jsonify({"error": "批注内容需为 1～2000 字"}), 400
    visitor = str(d.get("visitor") or "").strip()[:60] or "访客"
    with config.as_user(share["user_id"]):
        cid = db.add_share_comment(share["id"], share["experiment_id"],
                                   content, visitor=visitor)
        db.add_audit(share["experiment_id"], "share", "访客批注",
                     {"comment_id": cid, "visitor": visitor, "content": content},
                     decision="外部来源")
    return jsonify({"ok": True, "id": cid})


# ---------------- 设置 ----------------

@api_bp.get("/api/settings")
def api_get_settings():
    cfg = config.load_config()
    out = json.loads(json.dumps(cfg))
    out["llm"]["api_key"] = config.mask_secret(out["llm"]["api_key"])
    return jsonify(out)


@api_bp.post("/api/settings")
def api_save_settings():
    d = request.get_json(force=True)
    patch = {}
    llm_d = d.get("llm") or {}
    if llm_d:
        cur = config.load_config()["llm"]
        patch["llm"] = {}
        for k in ("base_url", "model"):
            if k in llm_d and llm_d[k] is not None:
                patch["llm"][k] = llm_d[k].strip()
        if "temperature" in llm_d:
            patch["llm"]["temperature"] = float(llm_d["temperature"])
        if "timeout" in llm_d:
            patch["llm"]["timeout"] = int(llm_d["timeout"])
        for k in ("max_retries", "retry_base_sec", "circuit_break_after"):
            if k in llm_d:
                patch["llm"][k] = float(llm_d[k]) if k == "retry_base_sec" else int(llm_d[k])
        if "stream" in llm_d:
            patch["llm"]["stream"] = bool(llm_d["stream"])
        if "fallback_models" in llm_d:
            fb = llm_d["fallback_models"]
            patch["llm"]["fallback_models"] = [str(x).strip() for x in fb if str(x).strip()] if isinstance(fb, list) else []
        key = (llm_d.get("api_key") or "").strip()
        if key and "*" not in key:
            patch["llm"]["api_key"] = key
        elif not key:
            patch["llm"]["api_key"] = cur.get("api_key", "")
    mon = d.get("monitor") or {}
    if mon:
        patch["monitor"] = {}
        for k in ("interval_sec", "stall_warn_sec"):
            if k in mon:
                patch["monitor"][k] = int(mon[k])
    kaka_d = d.get("kaka") or {}
    if kaka_d:
        cur_kaka = (config.load_config().get("kaka") or {})
        patch["kaka"] = {}
        for k in ("model", "base_url"):
            if k in kaka_d:
                patch["kaka"][k] = str(kaka_d[k] or "").strip()
        if "api_key" in kaka_d:
            key = str(kaka_d["api_key"] or "").strip()
            if key and "*" not in key:
                patch["kaka"]["api_key"] = key
            elif not key:
                patch["kaka"]["api_key"] = cur_kaka.get("api_key", "")
        if "temperature" in kaka_d:
            patch["kaka"]["temperature"] = float(kaka_d["temperature"])
        if "max_tokens" in kaka_d:
            patch["kaka"]["max_tokens"] = int(kaka_d["max_tokens"])
        if "enabled" in kaka_d:
            patch["kaka"]["enabled"] = bool(kaka_d["enabled"])
    ds_d = d.get("dataset_search") or {}
    if ds_d:
        patch["dataset_search"] = {}
        if "enabled" in ds_d:
            patch["dataset_search"]["enabled"] = bool(ds_d["enabled"])
        if "prefer_top_venues" in ds_d:
            patch["dataset_search"]["prefer_top_venues"] = bool(ds_d["prefer_top_venues"])
        if "sources" in ds_d:
            src = ds_d["sources"]
            patch["dataset_search"]["sources"] = [str(x) for x in src] if isinstance(src, list) else []
        for k in ("timeout", "max_papers", "max_links", "year_from", "year_to"):
            if k in ds_d:
                patch["dataset_search"][k] = float(ds_d[k]) if k == "timeout" else int(ds_d[k])
    notify_d = d.get("notify") or {}
    if notify_d:
        patch["notify"] = {}
        for k in ("webhook_url", "quiet_hours"):
            if k in notify_d:
                patch["notify"][k] = str(notify_d[k] or "").strip()
        if "enabled" in notify_d:
            patch["notify"]["enabled"] = bool(notify_d["enabled"])
        if "events" in notify_d:
            ev = notify_d["events"]
            patch["notify"]["events"] = [str(x) for x in ev] if isinstance(ev, list) else []
    config.save_user_config(g._auth_user["id"], patch)
    return jsonify({"ok": True, "llm_configured": llm.is_configured(),
                    "kaka_ready": kaka._llm_ready()})


@api_bp.post("/api/notify/test")
def api_test_notify():
    d = request.get_json(force=True) or {}
    url = str(d.get("webhook_url") or "").strip()
    if not url:
        cfg = config.load_config().get("notify") or {}
        url = cfg.get("webhook_url", "")
    if not url:
        return jsonify({"ok": False, "error": "请先填写 Webhook 地址"}), 400
    try:
        from core import notify
        payload = notify._payload("finished", "测试通知", "这是一条来自科研实验助手的测试消息。", "")
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code >= 400:
            return jsonify({"ok": False, "error": f"Webhook 返回 HTTP {r.status_code}"}), 200
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": crypto.redact(str(e))}), 200


@api_bp.post("/api/llm/test")
def api_test_llm():
    try:
        r = llm.chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=16, temperature=0)
        return jsonify({"ok": True, "reply": r.strip()[:50]})
    except Exception as e:
        return jsonify({"ok": False, "error": crypto.redact(str(e))}), 200


@api_bp.post("/api/kaka/ask")
def api_kaka_global_ask():
    """全局卡卡入口：可问任何问题，实验信息只作轻量参考。"""
    d = request.get_json(force=True) or {}
    return jsonify(kaka.ask_global(g._auth_user["id"], d.get("question")))


@api_bp.post("/api/kaka/chat")
def api_kaka_chat():
    d = request.get_json(force=True) or {}
    return jsonify(kaka.chat(g._auth_user["id"], d.get("message"), d.get("scope") or "global"))


@api_bp.get("/api/kaka/history")
def api_kaka_history():
    scope = request.args.get("scope") or "global"
    return jsonify({"items": kaka.history(g._auth_user["id"], scope,
                                          limit=int(request.args.get("limit") or 50))})


@api_bp.delete("/api/kaka/history")
def api_kaka_history_clear():
    scope = request.args.get("scope") or "global"
    return jsonify(kaka.clear_history(g._auth_user["id"], scope))


@api_bp.post("/api/kaka/test")
def api_test_kaka():
    cfg = kaka._llm_cfg()
    if not cfg.get("api_key"):
        return jsonify({"ok": False, "error": "卡卡没有可用 Key：填写卡卡专用 Key，或留空跟随主模型"})
    try:
        r = llm.chat([{"role": "user", "content": "只回复两个字：可用"}],
                     system="你是科研助手卡卡，现在测试连通性。",
                     cfg_override=cfg, max_tokens=256, temperature=0)
        return jsonify({"ok": True, "reply": r.strip()[:50], "model": cfg["model"]})
    except Exception as e:
        return jsonify({"ok": False, "error": crypto.redact(str(e))}), 200


# ---------------- AI 可信 / 评测 / 工具沙箱 ----------------

@api_bp.get("/api/ai/eval")
def api_ai_eval_status():
    uid = g._auth_user["id"]
    return jsonify({
        "cases": len(db.list_ai_eval_cases(uid=uid)),
        "runs": ai_eval.latest(uid=uid, limit=10),
    })


@api_bp.post("/api/ai/eval/run")
def api_ai_eval_run():
    uid = g._auth_user["id"]
    return jsonify(ai_eval.run(uid=uid))


@api_bp.get("/api/ai/tools")
def api_ai_tools_list():
    return jsonify({"tools": ai_tools.describe()})


@api_bp.post("/api/ai/tools/call")
def api_ai_tools_call():
    d = request.get_json(force=True) or {}
    name = str(d.get("tool") or "")
    result = ai_tools.call(
        g._auth_user["id"], name,
        args=d.get("args") or {}, confirm=bool(d.get("confirm")))
    return jsonify(result), (200 if result.get("ok") or result.get("needs_confirm") else 400)


@api_bp.get("/api/ai/tools/audit")
def api_ai_tools_audit():
    return jsonify({"items": db.list_ai_tool_audit(uid=g._auth_user["id"], limit=50)})


# ---------------- 卡卡智能体：实验副驾 / 项目群聊 / 数据集 ----------------
@api_bp.get("/api/experiments/<int:eid>/kaka")
def api_kaka_workspace(eid):
    own_exp_or_404(eid)
    ws = kaka.workspace(eid, uid=g._auth_user["id"])
    if not ws:
        abort(404)
    return jsonify(ws)


@api_bp.post("/api/experiments/<int:eid>/kaka/ask")
def api_kaka_ask(eid):
    own_exp_or_404(eid)
    d = request.get_json(force=True) or {}
    return jsonify(kaka.ask(g._auth_user["id"], d.get("question"), eid=eid))


@api_bp.post("/api/experiments/<int:eid>/kaka/prepare")
def api_kaka_prepare(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    feedback = str(d.get("feedback") or "")[:2000]
    result = kaka.prepare_script(eid, uid=g._auth_user["id"], feedback=feedback)
    if result.get("error"):
        return jsonify(result), 403
    # 复用现有生成任务：方案未就绪时先写方案，再写脚本
    if "plan" in result["steps"]:
        bg(eid, "plan", _job_plan, feedback or None)
    bg(eid, "code", _job_code, feedback or None)
    return jsonify(result)


@api_bp.post("/api/experiments/<int:eid>/kaka/summarize")
def api_kaka_summarize(eid):
    own_exp_or_404(eid, "write")
    d = request.get_json(force=True) or {}
    rid = int(d.get("run_id") or 0) or None
    result = kaka.summarize_result(eid, rid, uid=g._auth_user["id"])
    return jsonify(result), (200 if result.get("ok") else 400)


@api_bp.get("/api/projects/<int:pid>/messages")
def api_project_messages(pid):
    own_project_or_404(pid)
    return jsonify({"items": db.list_project_messages(pid, uid=g._auth_user["id"],
                                                      limit=int(request.args.get("limit") or 100))})


@api_bp.post("/api/projects/<int:pid>/messages")
def api_project_message_add(pid):
    own_project_or_404(pid, "write")
    d = request.get_json(force=True) or {}
    content = str(d.get("content") or "").strip()
    if not content:
        return jsonify({"error": "消息不能为空"}), 400
    mid = db.add_project_message(pid, "user", content[:2000],
                                 {"exp_id": int(d.get("exp_id") or 0)}, uid=g._auth_user["id"])
    return jsonify({"ok": True, "id": mid})


@api_bp.post("/api/projects/<int:pid>/kaka/ask")
def api_project_kaka_ask(pid):
    own_project_or_404(pid)
    d = request.get_json(force=True) or {}
    eid = int(d.get("experiment_id") or 0) or None
    result = kaka.ask(g._auth_user["id"], d.get("question"), eid=eid, pid=pid)
    db.add_project_message(pid, "kaka", str(result.get("answer") or "")[:2000],
                           {"tier": result.get("tier"), "experiment_id": eid},
                           uid=g._auth_user["id"])
    return jsonify(result)


@api_bp.get("/api/projects/<int:pid>/datasets")
def api_project_datasets(pid):
    own_project_or_404(pid)
    return jsonify({"items": db.list_project_datasets(pid, uid=g._auth_user["id"])})


@api_bp.post("/api/projects/<int:pid>/datasets")
def api_project_dataset_add(pid):
    own_project_or_404(pid, "write")
    d = request.get_json(force=True) or {}
    did = db.add_project_dataset(
        pid, d.get("name"), uid=g._auth_user["id"], version=str(d.get("version") or ""),
        paper=str(d.get("paper") or ""), evidence=str(d.get("evidence") or ""),
        url=str(d.get("url") or ""), url_type=str(d.get("url_type") or ""),
        license_name=str(d.get("license") or ""), status=str(d.get("status") or "candidate"))
    if not did:
        return jsonify({"error": "数据集名称不能为空"}), 400
    return jsonify({"ok": True, "id": did})


@api_bp.post("/api/projects/<int:pid>/datasets/collect")
def api_project_datasets_collect(pid):
    own_project_or_404(pid, "write")
    d = request.get_json(force=True) or {}
    result = kaka.collect_datasets(
        pid, d.get("query"), d.get("papers"), uid=g._auth_user["id"],
        year_from=int(d.get("year_from") or 0) or None,
        year_to=int(d.get("year_to") or 0) or None)
    return jsonify(result), (200 if result.get("ok") else 403)


@api_bp.post("/api/projects/<int:pid>/datasets/adopt")
def api_project_datasets_adopt(pid):
    own_project_or_404(pid, "write")
    d = request.get_json(force=True) or {}
    return jsonify(kaka.adopt_datasets(pid, d.get("candidates"), uid=g._auth_user["id"]))


@api_bp.put("/api/projects/<int:pid>/datasets/<int:did>")
def api_project_dataset_update(pid, did):
    own_project_or_404(pid, "write")
    d = request.get_json(force=True) or {}
    allowed = {k: d[k] for k in ("name", "version", "paper", "evidence", "url", "url_type",
                                 "license", "status") if k in d}
    if not db.update_project_dataset(did, pid, allowed, uid=g._auth_user["id"]):
        return jsonify({"error": "没有可更新的字段或数据集不存在"}), 400
    return jsonify({"ok": True})


@api_bp.delete("/api/projects/<int:pid>/datasets/<int:did>")
def api_project_dataset_delete(pid, did):
    own_project_or_404(pid, "write")
    db.delete_project_dataset(did, pid, uid=g._auth_user["id"])
    return jsonify({"ok": True})


# ---------------- 用户管理（管理员） ----------------

def _require_admin():
    u = auth.current_user()
    return u and u["role"] == "admin"


@api_bp.post("/api/users")
def api_add_user():
    if not _require_admin():
        return jsonify({"error": "仅管理员可创建账号"}), 403
    d = request.get_json(force=True) or {}
    username = (d.get("username") or "").strip()
    password = d.get("password") or ""
    if len(username) < 2 or len(password) < 6:
        return jsonify({"error": "用户名至少 2 个字符，密码至少 6 位"}), 400
    if db.get_user_by_name(username):
        return jsonify({"error": "用户名已存在"}), 400
    role = "admin" if d.get("role") == "admin" else "member"
    uid = db.add_user(username, auth.hash_password(password), role=role)
    return jsonify({"id": uid, "username": username, "role": role})


@api_bp.delete("/api/users/<int:uid>")
def api_del_user(uid):
    if not _require_admin():
        return jsonify({"error": "仅管理员可删除账号"}), 403
    if uid == g._auth_user["id"]:
        return jsonify({"error": "不能删除自己"}), 400
    target = db.get_user(uid)
    if not target:
        return jsonify({"error": "用户不存在"}), 404
    admins = [u for u in db.list_users() if u["role"] == "admin"]
    if target["role"] == "admin" and len(admins) <= 1:
        return jsonify({"error": "不能删除最后一个管理员"}), 400
    db.delete_user(uid)
    return jsonify({"ok": True})


@api_bp.post("/api/users/<int:uid>/password")
def api_user_password(uid):
    u = auth.current_user()
    if not u or (u["role"] != "admin" and u["id"] != uid):
        return jsonify({"error": "无权修改该账号密码"}), 403
    d = request.get_json(force=True) or {}
    new_pw = d.get("password") or ""
    if len(new_pw) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    db.update_user(uid, password_hash=auth.hash_password(new_pw))
    return jsonify({"ok": True})


@api_bp.post("/api/users/me/token")
def api_my_token():
    """当前用户（含普通成员）自行签发 CLI token。"""
    u = auth.current_user()
    return jsonify({"token": auth.issue_token(u["id"])})


@api_bp.post("/api/users/me/password")
def api_change_my_password():
    """用户自助改密码：必须校验当前密码。"""
    u = auth.current_user()
    d = request.get_json(force=True) or {}
    old = str(d.get("old_password") or "")
    new = str(d.get("new_password") or d.get("password") or "")
    confirm = str(d.get("confirm") or "")
    if not auth.verify_password(u, old):
        return jsonify({"error": "当前密码不正确"}), 400
    if new != confirm:
        return jsonify({"error": "两次输入的新密码不一致"}), 400
    if not 6 <= len(new) <= 128:
        return jsonify({"error": "新密码长度需为 6-128 位"}), 400
    if new == old:
        return jsonify({"error": "新密码不能与当前密码相同"}), 400
    db.update_user(u["id"], password_hash=auth.hash_password(new))
    return jsonify({"ok": True})


@api_bp.get("/api/profile")
def api_get_profile():
    u = auth.current_user()
    return jsonify(db.get_profile(u["id"]))


@api_bp.post("/api/profile")
def api_save_profile():
    """保存当前用户的科研档案；只接受白名单字段，防止写入敏感列。"""
    u = auth.current_user()
    d = request.get_json(force=True) or {}
    profile = {}
    for key in _PROFILE_FIELDS:
        value = d.get(key)
        if isinstance(value, (dict, list)):
            return jsonify({"error": f"{key} 字段格式不正确"}), 400
        value = str(value or "").strip()
        if len(value) > 2000:
            return jsonify({"error": f"{key} 内容不能超过 2000 字"}), 400
        profile[key] = value
    db.save_profile(u["id"], profile)
    return jsonify({"ok": True, "profile": profile})


# ---------------- 注册 Blueprint ----------------
app.register_blueprint(api_bp)


# ---------------- 启动 ----------------

def bootstrap():
    """重启恢复：心跳仍在的 run 重新挂监控，心跳超时的直接判失联。

    旧库没有 heartbeat_at（值为 NULL）时按有心跳处理并立即写一次，
    由后续 monitor 循环接管；只有明确超时的才标记 lost。
    """
    for r in db.active_runs():
        uid = r.get("user_id") or 1
        with config.as_user(uid):
            if r["status"] not in ("running", "setup", "uploading"):
                continue
            # 没有 remote_dir 说明还在部署阶段就重启了，无法恢复监控
            if not r.get("remote_dir"):
                db.update_run(r["id"], status="lost", finished_at=db.now())
                bus.note(r["experiment_id"], "服务在部署阶段重启，无法恢复该任务", "warn", r["id"])
                continue
            try:
                host = db.get_host(r["host_id"], include_secrets=True)
                if not host:
                    db.update_run(r["id"], status="lost", finished_at=db.now())
                    continue
                client = ssh_runner.connect(host)
                sftp = client.open_sftp()
                exited, code = monitor._read_exit(sftp, r["remote_dir"])
                alive = monitor._is_alive(sftp, r["remote_dir"])
                sftp.close()
                client.close()
                if exited:
                    db.update_run(r["id"], status="finished" if code == "0" else "failed",
                                  finished_at=db.now(), exit_code=str(code))
                    db.update_experiment(r["experiment_id"],
                                         status="done" if code == "0" else "failed")
                elif alive:
                    monitor.watch(r["id"], r["experiment_id"], r["host_id"],
                                  r["remote_dir"], uid=uid)
                    db.touch_run(r["id"])
                    print(f"[bootstrap] 恢复监控 run={r['id']}")
                else:
                    db.update_run(r["id"], status="lost", finished_at=db.now())
                    db.update_experiment(r["experiment_id"], status="failed")
            except Exception as e:
                print(f"[bootstrap] run={r['id']} 恢复失败: {e}")
                db.update_run(r["id"], status="lost", finished_at=db.now())


def _reap_stale_runs(loop=True):
    """周期性检查心跳超时的运行任务：线程还活着就跳过，否则重新挂载或判失联。

    单进程内 monitor._running 是真相，心跳只是跨重启的补充信号。
    """
    interval = 30
    timeout = 120
    while True:
        time.sleep(interval)
        try:
            for r in db.stale_running_runs(heartbeat_timeout=timeout):
                if (r.get("remote_dir") or "").startswith("~/demo_run"):
                    continue    # 演示数据：没有真实远程进程，跳过失联巡检
                if monitor.is_watching(r["id"]):
                    continue    # 监控线程还在，只是心跳写失败，放过
                uid = r.get("user_id") or 1
                with config.as_user(uid):
                    if r.get("remote_dir"):
                        try:
                            host = db.get_host(r["host_id"], include_secrets=True)
                            if host:
                                client = ssh_runner.connect(host, timeout=15)
                                sftp = client.open_sftp()
                                exited, code = monitor._read_exit(sftp, r["remote_dir"])
                                alive = monitor._is_alive(sftp, r["remote_dir"])
                                sftp.close()
                                client.close()
                                if exited:
                                    status = "finished" if code == "0" else "failed"
                                    db.update_run(r["id"], status=status, finished_at=db.now(),
                                                  exit_code=str(code))
                                    db.update_experiment(r["experiment_id"],
                                                         status="done" if status == "finished" else "failed")
                                    continue
                                if alive:
                                    monitor.watch(r["id"], r["experiment_id"], r["host_id"],
                                                  r["remote_dir"], uid=uid)
                                    db.touch_run(r["id"])
                                    print(f"[reaper] 重新挂载监控 run={r['id']}")
                                    continue
                        except Exception:
                            pass
                    # 连不上或进程已消失：标记失联
                    db.update_run(r["id"], status="lost", finished_at=db.now())
                    db.update_experiment(r["experiment_id"], status="failed")
                    bus.note(r["experiment_id"], "监控线程失联且无法恢复，任务标记为失联",
                             "error", r["id"])
        except Exception as e:
            print(f"[reaper] 巡检异常: {e}", flush=True)
        if not loop:
            break


if __name__ == "__main__":
    cfg = config.load_config()["server"]
    port = int(os.environ.get("PORT", cfg["port"]))
    scheme = os.environ.get("REKIT_SCHEME", cfg.get("scheme", "https")).lower()
    bootstrap()
    threading.Thread(target=_reap_stale_runs, daemon=True, name="stale-reaper").start()
    ssl_context = None
    if scheme == "https":
        cert, key = crypto.ensure_tls_cert(
            os.path.join(ROOT_DIR, cfg.get("ssl_cert", "data/certs/local-cert.pem")),
            os.path.join(ROOT_DIR, cfg.get("ssl_key", "data/certs/local-key.pem")))
        ssl_context = (cert, key)
    print(f"\n  科研实验助手已启动 → {scheme}://{cfg['host']}:{port}\n")
    app.run(host=cfg["host"], port=port, threaded=True, debug=False,
            use_reloader=False, ssl_context=ssl_context)
