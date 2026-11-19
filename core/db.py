"""SQLite 数据层：用户、主机、实验、运行记录、指标、事件。

多用户隔离：hosts / experiments / runs / events 都带 user_id。
业务函数默认按 config.current_uid() 过滤；后台线程入口用 config.as_user(uid) 包裹。
显式传 uid=... 可绕过当前上下文（仅 bootstrap / 迁移等内部场景使用）。
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

import config
from core import crypto
from config import DB_PATH, ensure_dirs

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'member',          -- admin | member
    api_token_hash TEXT DEFAULT '',       -- CLI/脚本用 token 的 sha256
    token_salt TEXT DEFAULT '',           -- token 派生盐
    created_at REAL,
    last_login REAL,
    profile_json TEXT DEFAULT ''        -- 个人主页资料（按用户隔离）
);

CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER DEFAULT 22,
    username TEXT NOT NULL,
    auth_type TEXT DEFAULT 'password',   -- password | key
    password TEXT DEFAULT '',
    key_path TEXT DEFAULT '',
    workdir TEXT DEFAULT '~/research-runs',
    python_bin TEXT DEFAULT 'python3',
    gpu INTEGER DEFAULT 0,               -- 是否需要采集 GPU 状态
    created_at REAL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    innovation TEXT NOT NULL,            -- 用户输入的创新点原文
    plan_json TEXT DEFAULT '',           -- LLM 产出的实验方案
    code_json TEXT DEFAULT '',           -- LLM 产出的代码文件集
    status TEXT DEFAULT 'draft',         -- draft|planned|coded|running|done|failed
    mode TEXT DEFAULT 'autonomous',      -- autonomous|copilot
    checkpoint TEXT DEFAULT '',          -- 当前等待确认的阶段
    criteria_json TEXT DEFAULT '{}',     -- 预注册判据
    criteria_locked INTEGER DEFAULT 0,
    criteria_locked_at REAL,
    criteria_history_json TEXT DEFAULT '[]',
    template_id TEXT DEFAULT '',         -- 来源实验模板
    forked_from_experiment_id INTEGER,   -- 派生自哪个实验
    forked_from_run_id INTEGER,          -- 派生自哪次运行（对应 git commit）
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    experiment_id INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    remote_dir TEXT DEFAULT '',
    local_dir TEXT DEFAULT '',
    pid TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',       -- pending|uploading|setup|running|finished|failed|lost
    exit_code TEXT DEFAULT '',
    started_at REAL,
    finished_at REAL,
    log_tail TEXT DEFAULT '',
    metrics_json TEXT DEFAULT '',
    summary_json TEXT DEFAULT '',
    git_sha TEXT DEFAULT '',             -- run 前锁定的代码快照 commit
    env_json TEXT DEFAULT '',            -- seed / 依赖 / 硬件环境快照
    heartbeat_at REAL                    -- 监控线程最近一次心跳，用于判定僵尸任务
);

CREATE TABLE IF NOT EXISTS git_remotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    provider TEXT DEFAULT 'github',
    repo_full_name TEXT NOT NULL,          -- owner/repo
    token_enc TEXT DEFAULT '',             -- crypto.encrypt 加密后的 PAT
    token_hint TEXT DEFAULT '',            -- 尾 4 位，仅用于展示
    default_branch TEXT DEFAULT 'main',
    created_at REAL,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    step REAL,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id, name);

CREATE TABLE IF NOT EXISTS terminal_tickets (
    token_hash TEXT PRIMARY KEY,
    uid INTEGER NOT NULL,
    host_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    read_only INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    experiment_id INTEGER,
    run_id INTEGER,
    kind TEXT DEFAULT 'log',             -- log|stage|metric|gpu|error|summary
    level TEXT DEFAULT 'info',
    message TEXT DEFAULT '',
    ts REAL
);

-- SSE 事件日志：所有实时事件落库，浏览器断线后按 id 补发，重启不丢
CREATE TABLE IF NOT EXISTS stream_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    experiment_id INTEGER NOT NULL,
    run_id INTEGER,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts REAL
);
CREATE INDEX IF NOT EXISTS idx_stream_exp ON stream_events(experiment_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    experiment_id INTEGER,
    run_id INTEGER,
    stage TEXT DEFAULT '',
    action TEXT DEFAULT '',
    decision TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    detail_json TEXT DEFAULT '',
    ts REAL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    experiment_id INTEGER,
    run_id INTEGER,
    stage TEXT DEFAULT '',
    model TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    request_id TEXT DEFAULT '',
    ts REAL
);
"""

# 依赖 user_id 列的索引：必须在老库 ALTER 补列之后再建，否则迁移时会报
# "no such column: user_id"。全新建库时也能正常创建。
USER_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hosts_user ON hosts(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_exp_user ON experiments(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_exp ON runs(experiment_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_exp ON events(experiment_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_exp ON audit_log(experiment_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_exp ON usage_log(experiment_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_stream_exp ON stream_events(experiment_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_stream_run ON stream_events(run_id)",
]


def get_conn():
    if getattr(_local, "conn", None) is None:
        ensure_dirs()
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _local.conn = conn
    return _local.conn


def _columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    """建表 + 老库迁移：补齐 user_id、profile、git、审计与复现字段。"""
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    # 兼容早期没有 user_id 的库：先补列，再建依赖该列的索引
    for table in ("hosts", "experiments", "runs", "events", "audit_log", "usage_log", "stream_events"):
        if "user_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    if "profile_json" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN profile_json TEXT DEFAULT ''")
    experiment_cols = (
        ("forked_from_experiment_id", "INTEGER"),
        ("forked_from_run_id", "INTEGER"),
        ("mode", "TEXT DEFAULT 'autonomous'"),
        ("checkpoint", "TEXT DEFAULT ''"),
        ("criteria_json", "TEXT DEFAULT '{}'"),
        ("criteria_locked", "INTEGER DEFAULT 0"),
        ("criteria_locked_at", "REAL"),
        ("criteria_history_json", "TEXT DEFAULT '[]'"),
        ("template_id", "TEXT DEFAULT ''"),
    )
    for col, ddl in experiment_cols:
        if col not in _columns(conn, "experiments"):
            conn.execute(f"ALTER TABLE experiments ADD COLUMN {col} {ddl}")
    for col, ddl in (("git_sha", "TEXT DEFAULT ''"), ("env_json", "TEXT DEFAULT ''"),
                     ("heartbeat_at", "REAL"), ("remote_url", "TEXT DEFAULT ''"),
                     ("remote_sha", "TEXT DEFAULT ''")):
        if col not in _columns(conn, "runs"):
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
    for table in ("git_remotes",):
        if "user_id" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    for sql in USER_INDEXES:
        conn.execute(sql)
    conn.commit()


def q(sql, args=()):
    return get_conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    r = get_conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    conn = get_conn()
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.lastrowid


def now():
    return time.time()


def _uid(explicit=None):
    """解析当前用户 id：显式参数 > 上下文 > 默认 admin(1)。

    注意：返回 1 只表示"读系统默认视图"，调用方仍应在鉴权层拦住未登录请求。
    """
    if explicit is not None:
        return int(explicit)
    u = config.current_uid()
    return int(u) if u is not None else 1


# ---------- users ----------
def get_user(uid):
    return q1("SELECT * FROM users WHERE id=?", (uid,))


def get_user_by_name(username):
    return q1("SELECT * FROM users WHERE username=?", (username,))


def list_users():
    rows = q("SELECT id,username,role,created_at,last_login FROM users ORDER BY id")
    return [dict(r) for r in rows]


def count_users():
    r = q1("SELECT COUNT(*) AS n FROM users")
    return r["n"] if r else 0


def add_user(username, password_hash, role="member"):
    return ex(
        "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
        (username, password_hash, role, now()))


def get_profile(uid):
    u = get_user(uid)
    if not u:
        return {}
    try:
        return json.loads(u.get("profile_json") or "{}") or {}
    except Exception:
        return {}


def save_profile(uid, profile):
    ex("UPDATE users SET profile_json=? WHERE id=?",
       (json.dumps(profile or {}, ensure_ascii=False), uid))


def update_user(uid, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE users SET {cols} WHERE id=?", list(fields.values()) + [uid])


def delete_user(uid):
    """删除用户及其全部数据（含工作区），禁止删最后一个管理员由上层校验。"""
    import shutil
    from config import WORKSPACE_DIR
    exp_ids = [r["id"] for r in q("SELECT id FROM experiments WHERE user_id=?", (uid,))]
    for eid in exp_ids:
        shutil.rmtree(os.path.join(WORKSPACE_DIR, str(eid)), ignore_errors=True)
    for table in ("metrics",):
        ex(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE user_id=?)", (uid,))
    for table in ("events", "audit_log", "usage_log", "runs", "experiments", "hosts"):
        ex(f"DELETE FROM {table} WHERE user_id=?", (uid,))
    ex("DELETE FROM users WHERE id=?", (uid,))
    import shutil as _sh
    _sh.rmtree(os.path.join(config.USERS_DIR, str(uid)), ignore_errors=True)
    return exp_ids


# ---------- hosts ----------
_HOST_PUBLIC_COLS = ("id", "user_id", "name", "host", "port", "username", "auth_type",
                     "workdir", "python_bin", "gpu", "created_at")


# ---------- github remote credentials ----------
def list_git_remotes(uid=None):
    return [dict(r) for r in q(
        "SELECT * FROM git_remotes WHERE user_id=? ORDER BY id DESC", (_uid(uid),))]


def get_git_remote(rid, uid=None):
    row = q1("SELECT * FROM git_remotes WHERE id=? AND user_id=?", (rid, _uid(uid)))
    return dict(row) if row else None


def get_git_remote_by_repo(repo_full_name, uid=None):
    row = q1("SELECT * FROM git_remotes WHERE repo_full_name=? AND user_id=?",
             (repo_full_name, _uid(uid)))
    return dict(row) if row else None


def add_git_remote(repo_full_name, token_enc, token_hint, uid=None,
                   provider="github", default_branch="main"):
    return ex(
        """INSERT INTO git_remotes(user_id,provider,repo_full_name,token_enc,token_hint,
           default_branch,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
        (_uid(uid), provider, repo_full_name, token_enc, token_hint,
         default_branch, now(), now()))


def update_git_remote(rid, uid=None, **fields):
    allowed = ("repo_full_name", "token_enc", "token_hint", "default_branch")
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            args.append(v)
    if not sets:
        return 0
    sets.append("updated_at=?")
    args.append(now())
    args.extend((rid, _uid(uid)))
    return ex(f"UPDATE git_remotes SET {','.join(sets)} WHERE id=? AND user_id=?", tuple(args))


def delete_git_remote(rid, uid=None):
    return ex("DELETE FROM git_remotes WHERE id=? AND user_id=?", (rid, _uid(uid)))


def _host_public(row):
    return {k: row[k] for k in _HOST_PUBLIC_COLS} if row else None


def list_hosts(uid=None):
    rows = q("SELECT * FROM hosts WHERE user_id=? ORDER BY id DESC", (_uid(uid),))
    return [_host_public(r) for r in rows]


def get_host(hid, uid=None, include_secrets=False):
    row = q1("SELECT * FROM hosts WHERE id=? AND user_id=?", (hid, _uid(uid)))
    if not row:
        return None
    if not include_secrets:
        return _host_public(row)
    row = dict(row)
    row["password"] = crypto.decrypt(row.get("password"))
    row["key_path"] = crypto.decrypt(row.get("key_path"))
    return row


def add_host(d, uid=None):
    return ex(
        """INSERT INTO hosts(user_id,name,host,port,username,auth_type,password,key_path,workdir,python_bin,gpu,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_uid(uid), d["name"], d["host"], int(d.get("port") or 22), d["username"],
         d.get("auth_type", "password"), crypto.encrypt(d.get("password", "")),
         crypto.encrypt(d.get("key_path", "")),
         d.get("workdir", "~/research-runs"), d.get("python_bin", "python3"),
         int(d.get("gpu", 0)), now()))


def migrate_host_secrets():
    """把已有明文 SSH 密码 / 私钥路径原地加密；重复调用安全。"""
    rows = q("SELECT id,password,key_path FROM hosts")
    for row in rows:
        patch = {}
        for key in ("password", "key_path"):
            value = row[key] or ""
            if value and not str(value).startswith(crypto.PREFIX):
                patch[key] = crypto.encrypt(value)
        if patch:
            cols = ", ".join(f"{k}=?" for k in patch)
            ex(f"UPDATE hosts SET {cols} WHERE id=?", list(patch.values()) + [row["id"]])


def delete_host(hid, uid=None):
    ex("DELETE FROM hosts WHERE id=? AND user_id=?", (hid, _uid(uid)))


# ---------- terminal tickets ----------
def issue_terminal_ticket(uid, host_id, ttl_sec=60, read_only=False):
    token = secrets.token_urlsafe(24)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = now() + max(10, int(ttl_sec or 60))
    ex("DELETE FROM terminal_tickets WHERE expires_at < ?", (now() - 3600,))
    ex("""INSERT INTO terminal_tickets(token_hash,uid,host_id,expires_at,read_only)
          VALUES(?,?,?,?,?)""",
       (token_hash, uid, host_id, expires_at, 1 if read_only else 0))
    return token, expires_at


def consume_terminal_ticket(token, uid, host_id):
    """原子消费一次性终端票据；成功返回 {'read_only': bool}，失败返回 None。"""
    if not token:
        return None
    token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    conn = get_conn()
    ts = now()
    cur = conn.execute(
        """UPDATE terminal_tickets SET used_at=?
           WHERE token_hash=? AND uid=? AND host_id=? AND used_at IS NULL AND expires_at>?""",
        (ts, token_hash, uid, host_id, ts))
    conn.commit()
    if cur.rowcount == 0:
        return None
    row = q1("SELECT read_only FROM terminal_tickets WHERE token_hash=?", (token_hash,))
    ex("DELETE FROM terminal_tickets WHERE token_hash=?", (token_hash,))
    return {"read_only": bool(row and row["read_only"])}


# ---------- experiments ----------
def list_experiments(uid=None):
    rows = q("""SELECT e.*, (SELECT COUNT(*) FROM runs r WHERE r.experiment_id=e.id) AS run_count
                FROM experiments e WHERE e.user_id=? ORDER BY e.id DESC""", (_uid(uid),))
    out = []
    for r in rows:
        d = dict(r)
        d.pop("innovation", None)
        d.pop("plan_json", None)
        d.pop("code_json", None)
        out.append(d)
    return out


def get_experiment(eid, uid=None):
    return q1("SELECT * FROM experiments WHERE id=? AND user_id=?", (eid, _uid(uid)))


def create_experiment(title, innovation, uid=None, template_id=""):
    t = now()
    return ex("""INSERT INTO experiments(user_id,title,innovation,status,template_id,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?)""",
              (_uid(uid), title, innovation, "draft", template_id or "", t, t))


def update_experiment(eid, uid=None, **fields):
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE experiments SET {cols} WHERE id=? AND user_id=?",
       list(fields.values()) + [eid, _uid(uid)])


def delete_experiment(eid, uid=None):
    u = _uid(uid)
    ex("DELETE FROM metrics WHERE run_id IN (SELECT id FROM runs WHERE experiment_id=? AND user_id=?)", (eid, u))
    ex("DELETE FROM events WHERE experiment_id=? AND user_id=?", (eid, u))
    ex("DELETE FROM audit_log WHERE experiment_id=? AND user_id=?", (eid, u))
    ex("DELETE FROM usage_log WHERE experiment_id=? AND user_id=?", (eid, u))
    ex("DELETE FROM runs WHERE experiment_id=? AND user_id=?", (eid, u))
    ex("DELETE FROM experiments WHERE id=? AND user_id=?", (eid, u))


# ---------- runs ----------
def create_run(experiment_id, host_id, remote_dir, local_dir, uid=None, git_sha=""):
    return ex("""INSERT INTO runs(user_id,experiment_id,host_id,remote_dir,local_dir,status,started_at,git_sha)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (_uid(uid), experiment_id, host_id, remote_dir, local_dir, "pending", now(), git_sha or ""))


def get_run(rid, uid=None):
    return q1("SELECT * FROM runs WHERE id=? AND user_id=?", (rid, _uid(uid)))


def list_runs(experiment_id, uid=None):
    return [dict(r) for r in q(
        "SELECT * FROM runs WHERE experiment_id=? AND user_id=? ORDER BY id DESC",
        (experiment_id, _uid(uid)))]


def latest_run(experiment_id, uid=None):
    return q1("SELECT * FROM runs WHERE experiment_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
              (experiment_id, _uid(uid)))


def recent_runs(limit=8, uid=None):
    """首页最近运行：带实验标题，按 id 倒序。"""
    return [dict(r) for r in q(
        """SELECT r.*, e.title AS exp_title FROM runs r
           JOIN experiments e ON e.id = r.experiment_id AND e.user_id = r.user_id
           WHERE r.user_id=? ORDER BY r.id DESC LIMIT ?""",
        (_uid(uid), limit))]


def update_run(rid, uid=None, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    ex(f"UPDATE runs SET {cols} WHERE id=? AND user_id=?", list(fields.values()) + [rid, _uid(uid)])


def active_runs(uid=None):
    """uid=None 时查所有用户（仅 bootstrap 用）；显式 uid 只查该用户。"""
    if uid is None and config.current_uid() is None:
        return [dict(r) for r in q(
            "SELECT * FROM runs WHERE status IN ('pending','uploading','setup','running')")]
    return [dict(r) for r in q(
        "SELECT * FROM runs WHERE user_id=? AND status IN ('pending','uploading','setup','running')",
        (_uid(uid),))]


# ---------- metrics ----------
def add_metric(run_id, name, value, step=None):
    ex("INSERT INTO metrics(run_id,name,value,step,ts) VALUES(?,?,?,?,?)",
       (run_id, name, value, step, now()))


def metrics_series(run_id, limit=800):
    rows = q("""SELECT name,value,step,ts FROM metrics WHERE run_id=? ORDER BY id DESC LIMIT ?""",
             (run_id, limit * 8))
    series = {}
    for r in rows:
        series.setdefault(r["name"], []).append(
            {"v": r["value"], "s": r["step"], "t": r["ts"]})
    for k in series:
        series[k] = list(reversed(series[k]))[-limit:]
    return series


# ---------- events ----------
def add_event(experiment_id, run_id, message, kind="log", level="info", uid=None):
    eid = ex("INSERT INTO events(user_id,experiment_id,run_id,kind,level,message,ts) VALUES(?,?,?,?,?,?,?)",
             (_uid(uid), experiment_id, run_id, kind, level, message, now()))
    return eid


def events_after(experiment_id, last_id=0, limit=400, uid=None):
    return [dict(r) for r in q(
        "SELECT * FROM events WHERE experiment_id=? AND user_id=? AND id>? ORDER BY id ASC LIMIT ?",
        (experiment_id, _uid(uid), last_id, limit))]


def recent_events(run_id, limit=800):
    rows = q("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))
    return [dict(r) for r in reversed(rows)]


# ---------- stream events（SSE 事件日志，重启/断线可补发） ----------
def add_stream_event(experiment_id, event_type, payload, run_id=None, uid=None):
    """把一条实时事件落库，返回全局自增 id 供 SSE id: 字段使用。"""
    return ex(
        "INSERT INTO stream_events(user_id,experiment_id,run_id,type,payload,ts) VALUES(?,?,?,?,?,?)",
        (_uid(uid), experiment_id, run_id, event_type,
         json.dumps(payload, ensure_ascii=False), now()))


def stream_events_after(experiment_id, last_id=0, limit=500, uid=None):
    """读取游标之后落库的事件，用于 SSE 断线补发。"""
    return [dict(r) for r in q(
        "SELECT * FROM stream_events WHERE experiment_id=? AND user_id=? AND id>? "
        "ORDER BY id ASC LIMIT ?", (experiment_id, _uid(uid), last_id, limit))]


def stream_event_cursor(experiment_id, uid=None):
    r = q1("SELECT MAX(id) AS m FROM stream_events WHERE experiment_id=? AND user_id=?",
           (experiment_id, _uid(uid)))
    return (r and r["m"]) or 0


def prune_stream_events(experiment_id, keep=2000, uid=None):
    """清理实验的旧事件，只保留最近 keep 条，避免 journal 无限膨胀。"""
    ex("""DELETE FROM stream_events WHERE experiment_id=? AND user_id=? AND id <=
          (SELECT COALESCE(MAX(id),0)-? FROM stream_events WHERE experiment_id=? AND user_id=?)""",
       (experiment_id, _uid(uid), keep, experiment_id, _uid(uid)))


def touch_run(rid, uid=None):
    """监控线程每轮写一次心跳。"""
    ex("UPDATE runs SET heartbeat_at=? WHERE id=? AND user_id=?", (now(), rid, _uid(uid)))


def stale_running_runs(heartbeat_timeout=90, uid=None):
    """找出状态仍为运行中、但心跳超时的 run（含从未写过心跳的旧库记录）。"""
    cutoff = now() - heartbeat_timeout
    if uid is None and config.current_uid() is None:
        return [dict(r) for r in q(
            """SELECT * FROM runs WHERE status IN ('running','setup','uploading')
               AND (heartbeat_at IS NULL OR heartbeat_at < ?)""", (cutoff,))]
    return [dict(r) for r in q(
        """SELECT * FROM runs WHERE user_id=? AND status IN ('running','setup','uploading')
           AND (heartbeat_at IS NULL OR heartbeat_at < ?)""", (_uid(uid), cutoff))]


# ---------- audit / usage ----------
def add_audit(experiment_id, stage, action, detail=None, decision="", run_id=None,
              actor="", uid=None):
    return ex(
        """INSERT INTO audit_log(user_id,experiment_id,run_id,stage,action,decision,actor,detail_json,ts)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (_uid(uid), experiment_id, run_id, stage, action, decision, actor,
         json.dumps(detail or {}, ensure_ascii=False), now()))


def list_audits(experiment_id, limit=200, uid=None):
    rows = q("""SELECT * FROM audit_log WHERE experiment_id=? AND user_id=?
                ORDER BY id DESC LIMIT ?""", (experiment_id, _uid(uid), limit))
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["detail"] = json.loads(item.get("detail_json") or "{}")
        except Exception:
            item["detail"] = {}
        out.append(item)
    return out


def add_usage(stage, model="", prompt_tokens=0, completion_tokens=0,
              cost=0.0, request_id="", experiment_id=None, run_id=None, uid=None):
    return ex(
        """INSERT INTO usage_log(user_id,experiment_id,run_id,stage,model,prompt_tokens,
                                  completion_tokens,total_tokens,cost,request_id,ts)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (_uid(uid), experiment_id, run_id, stage, model, int(prompt_tokens or 0),
         int(completion_tokens or 0), int(prompt_tokens or 0) + int(completion_tokens or 0),
         float(cost or 0), request_id or "", now()))


def usage_summary(experiment_id=None, uid=None):
    args = [_uid(uid)]
    where = "user_id=?"
    if experiment_id is not None:
        where += " AND experiment_id=?"
        args.append(experiment_id)
    row = q1(f"""SELECT COUNT(*) AS calls,
                        COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                        COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                        COALESCE(SUM(total_tokens),0) AS total_tokens,
                        COALESCE(SUM(cost),0) AS cost
                 FROM usage_log WHERE {where}""", tuple(args))
    return row or {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                   "total_tokens": 0, "cost": 0}
