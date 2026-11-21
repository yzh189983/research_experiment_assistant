"""数据后端适配层：SQLite（默认）/ PostgreSQL / MySQL。

设计目标：
- 默认（storage.backend 未配置或为 sqlite）完全沿用原来的本地 SQLite 文件，
  core/db.py 的上层 SQL 与行为零变化。
- 配置 postgresql / mysql 后，通过「模拟 sqlite3.Connection 接口」的包装，
  让上层 1800+ 行业务 SQL 无需改动即可运行。适配层负责：
    1. 占位符 ? -> %s（psycopg2 / PyMySQL 均为 pyformat）
    2. DDL 方言：AUTOINCREMENT、REAL 精度、IF NOT EXISTS 索引
    3. INSERT OR REPLACE -> 各库 upsert
    4. PRAGMA -> information_schema；SQLite 专有 PRAGMA 直接忽略
    5. executescript -> 逐条执行（DDL 转换后）
    6. lastrowid：PostgreSQL 用 RETURNING id 补出
    7. 字符串拼接 || -> MySQL CONCAT
    8. 后端异常统一映射为 IntegrityError 等

驱动按需导入：只有真正启用外部后端时才 import psycopg2 / pymysql。
"""
import re
import threading

import config

_local = threading.local()

# 只在这些 SQL 上给 PostgreSQL 追加 RETURNING id；必须匹配 db.py 里的 INSERT 写法
_INSERT_RE = re.compile(r"^\s*INSERT\b", re.IGNORECASE)
_OR_REPLACE_RE = re.compile(
    r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+([\w]+)\s*\(([^)]*)\)\s*VALUES",
    re.IGNORECASE | re.DOTALL,
)
_PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\(\s*([\w]+)\s*\)\s*$", re.IGNORECASE)
_PRAGMA_IGNORE_RE = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)
# 表级唯一键：INSERT OR REPLACE 在 PostgreSQL 转成 ON CONFLICT 时需要
_TABLE_UNIQUE_KEYS = {
    "project_members": ("project_id", "user_id"),
    "kaka_skills": ("user_id", "skill_key"),
    "project_gpu_resources": ("project_id", "host_id", "gpu_index"),
}


class StorageError(RuntimeError):
    """后端配置/连接错误。"""


# 后端异常统一别名：app.py 等处只需要 catch 这一个
try:
    import sqlite3 as _sqlite3
    IntegrityError = _sqlite3.IntegrityError
except ImportError:  # pragma: no cover
    class IntegrityError(Exception):
        pass


def _normalize_backend(value):
    v = str(value or "").strip().lower()
    if v in ("", "sqlite", "sqlite3", "local", "file"):
        return "sqlite"
    if v in ("postgres", "postgresql", "pg"):
        return "postgresql"
    if v in ("mysql", "mariadb"):
        return "mysql"
    raise StorageError(
        "storage.backend 仅支持 sqlite / postgresql / mysql，当前为：%r" % value)


_backend_config_cache = None


def backend_config():
    """读取系统级存储配置（只认全局配置，不受用户级覆盖影响）。

    进程内只解析一次并缓存：设置页保存新配置后不会立即影响当前进程的
    连接（否则正在运行的线程/请求会突然改连到新库而报错），必须重启服务
    才切换到新后端。测试可用 reset_backend_config() 清除缓存。
    """
    global _backend_config_cache
    if _backend_config_cache is None:
        cfg = config.load_global_config().get("storage") or {}
        _backend_config_cache = {
            "backend": _normalize_backend(cfg.get("backend")),
            "host": str(cfg.get("host") or "127.0.0.1"),
            "port": int(cfg.get("port") or 0),
            "database": str(cfg.get("database") or "rekit"),
            "user": str(cfg.get("user") or ""),
            "password": str(cfg.get("password") or ""),
            "sslmode": str(cfg.get("sslmode") or "disable"),
        }
    return dict(_backend_config_cache)


def reset_backend_config():
    """清除进程级配置缓存（仅供测试/切换后主动重载使用）。"""
    global _backend_config_cache
    _backend_config_cache = None


def _import_driver(backend):
    if backend == "postgresql":
        try:
            import psycopg2
            import psycopg2.extras
            return psycopg2
        except ImportError:
            raise StorageError("已配置 PostgreSQL 后端，但缺少驱动；请安装 psycopg2-binary。")
    if backend == "mysql":
        try:
            import pymysql
            import pymysql.cursors
            return pymysql
        except ImportError:
            raise StorageError("已配置 MySQL 后端，但缺少驱动；请安装 PyMySQL。")
    raise StorageError("未知后端：%s" % backend)


# ---------------------------------------------------------------------------
# SQL 方言转译
# ---------------------------------------------------------------------------

def translate_sql(sql, backend):
    """把 SQLite 风格 SQL 转成目标后端可执行的形式。"""
    if backend == "sqlite" or not sql:
        return sql, False, sql

    original = sql
    # INSERT OR REPLACE 必须在占位符替换前处理（它不带 ? 参数，但结构要整体改写）
    if backend in ("postgresql", "mysql") and _OR_REPLACE_RE.match(sql):
        sql = _translate_insert_or_replace(sql, backend)

    # 占位符：? -> %s（字符串字面量里的 ? 不动）
    sql = _replace_placeholders(sql)

    # MySQL 原生 || 是逻辑或，需转成 CONCAT
    if backend == "mysql" and "||" in sql:
        sql = _translate_concat(sql)

    # PostgreSQL 用 RETURNING id 补 lastrowid（仅 INSERT）
    returning_id = False
    if backend == "postgresql" and _INSERT_RE.match(sql) and "RETURNING" not in sql.upper():
        sql = sql.rstrip().rstrip(";") + " RETURNING id"
        returning_id = True

    return sql, returning_id, original


def _replace_placeholders(sql):
    """跳过单引号字符串，把 SQL 中的 ? 换成 %s。"""
    out = []
    in_str = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":  # SQL 转义 ''
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(ch)
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _translate_insert_or_replace(sql, backend):
    m = _OR_REPLACE_RE.match(sql)
    table = m.group(1)
    cols = [c.strip() for c in m.group(2).split(",") if c.strip()]
    # 正则已消耗到 VALUES 之后，rest 形如 "(?,?,'owner',?)"
    rest = sql[m.end():]
    if backend == "mysql":
        return "REPLACE INTO %s(%s) VALUES%s" % (table, ",".join(cols), rest)
    keys = _TABLE_UNIQUE_KEYS.get(table)
    if not keys:
        # 没有声明唯一键时退化为 ON CONFLICT DO NOTHING，至少不会写坏数据
        return "INSERT INTO %s(%s) VALUES%s ON CONFLICT DO NOTHING" % (
            table, ",".join(cols), rest)
    updates = ",".join("%s=EXCLUDED.%s" % (c, c) for c in cols if c not in keys)
    clause = " ON CONFLICT(%s) DO UPDATE SET %s" % (",".join(keys), updates) if updates \
        else " ON CONFLICT(%s) DO NOTHING" % ",".join(keys)
    return "INSERT INTO %s(%s) VALUES%s%s" % (table, ",".join(cols), rest, clause)


def _translate_concat(sql):
    """把 `a || b` 形式的字符串拼接改成 CONCAT(a, b)。

    本项目只有 list_experiments 的 tags 过滤用到（',' || COALESCE(...) || ','），
    这里用 Token 扫描处理引号与括号，避免误伤字符串字面量里的 ||。
    """
    if "||" not in sql:
        return sql
    tokens = []
    buf = []
    in_str = False
    depth = 0
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif not in_str and ch == "(":
            depth += 1
            buf.append(ch)
        elif not in_str and ch == ")":
            depth -= 1
            buf.append(ch)
        elif not in_str and ch == "|" and i + 1 < len(sql) and sql[i + 1] == "|":
            tokens.append("".join(buf))
            buf = []
            i += 2
            continue
        else:
            buf.append(ch)
        i += 1
    tokens.append("".join(buf))
    if len(tokens) == 1:
        return sql
    parts = [t.strip() for t in tokens]
    return "CONCAT(%s)" % ", ".join(parts)


def translate_ddl(script, backend):
    """把 SQLite SCHEMA 转成目标后端的 DDL。返回逐条语句列表。"""
    if backend == "sqlite":
        return [script]

    lines = []
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    if backend == "postgresql":
        body = body.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                            "INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY")
        body = body.replace(" REAL", " DOUBLE PRECISION")
    else:  # mysql
        body = body.replace("INTEGER PRIMARY KEY AUTOINCREMENT",
                            "INTEGER PRIMARY KEY AUTO_INCREMENT")
        body = body.replace(" REAL", " DOUBLE")
        body = re.sub(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS", "CREATE INDEX", body, flags=re.I)

    statements = [s.strip() for s in body.split(";") if s.strip()]
    return statements


# ---------------------------------------------------------------------------
# 连接包装：让外部后端看起来像 sqlite3.Connection
# ---------------------------------------------------------------------------

class _Cursor:
    """ psycopg2 / PyMySQL cursor 的最小包装，补齐 lastrowid。"""

    def __init__(self, raw, backend, returning_id=False):
        self._raw = raw
        self._backend = backend
        self._returning_id = returning_id
        self._lastrowid = None
        if returning_id:
            try:
                row = raw.fetchone()
                if row:
                    try:
                        self._lastrowid = row[0]
                    except (KeyError, IndexError, TypeError):
                        self._lastrowid = next(iter(row.values()))
            except Exception:
                self._lastrowid = None

    @property
    def lastrowid(self):
        if self._returning_id:
            return self._lastrowid
        return getattr(self._raw, "lastrowid", None) or 0

    @property
    def rowcount(self):
        return getattr(self._raw, "rowcount", -1)

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self._raw)


class _Connection:
    """模拟 sqlite3.Connection 的 execute/executescript/commit/close。"""

    def __init__(self, raw, backend, driver, database):
        self._raw = raw
        self._backend = backend
        self._driver = driver
        self._database = database
        self.row_factory = None

    def _cursor(self):
        if self._backend == "postgresql":
            return self._raw.cursor(cursor_factory=self._driver.extras.RealDictCursor)
        return self._raw.cursor()

    @staticmethod
    def _map_error(exc, backend):
        if backend == "postgresql":
            import psycopg2
            if isinstance(exc, psycopg2.Error):
                return IntegrityError(str(exc))
        else:
            import pymysql
            if isinstance(exc, pymysql.MySQLError):
                # 1062 唯一键冲突；1406 数据过长也归入完整性错误
                if getattr(exc, "args", ()) and exc.args[0] in (1062, 1406):
                    return IntegrityError(str(exc))
        return exc

    def execute(self, sql, args=()):
        backend = self._backend

        # PRAGMA table_info(t) -> information_schema 查询（db._columns 依赖 name 字段）
        m = _PRAGMA_TABLE_INFO_RE.match(sql or "")
        if m:
            table = m.group(1)
            if backend == "postgresql":
                sql = ("SELECT column_name AS name FROM information_schema.columns "
                       "WHERE table_schema = current_schema() AND table_name = %s")
            else:
                sql = ("SELECT column_name AS name FROM information_schema.columns "
                       "WHERE table_schema = DATABASE() AND table_name = %s")
            args = (table,)
        elif _PRAGMA_IGNORE_RE.match(sql or ""):
            return _Cursor(self._cursor(), backend)  # busy_timeout / journal_mode 等忽略

        translated, returning_id, _ = translate_sql(sql, backend)
        cur = self._cursor()
        try:
            cur.execute(translated, args if args else None)
        except Exception as exc:
            raise self._map_error(exc, backend)
        return _Cursor(cur, backend, returning_id)

    def executescript(self, script):
        statements = translate_ddl(script, self._backend)
        for stmt in statements:
            try:
                self.execute(stmt)
            except Exception as exc:
                # CREATE INDEX IF NOT EXISTS 在 MySQL 不支持，已 CREATE 时忽略
                if self._backend == "mysql" and stmt.upper().startswith("CREATE INDEX") \
                        and getattr(exc, "args", ()) and exc.args[0] == 1061:
                    continue
                raise
        self.commit()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def connect(db_path=None):
    """按配置创建连接。SQLite 路径保持原生行为；外部后端返回 _Connection。

    db_path 由 db.py 传入（其模块级 DB_PATH 可被测试重定向）。
    """
    st = backend_config()
    backend = st["backend"]

    if backend == "sqlite":
        # —— 这里必须与改造前的 db.get_conn 完全一致 ——
        import sqlite3
        config.ensure_dirs()
        conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn, backend

    driver = _import_driver(backend)
    kwargs = dict(host=st["host"], user=st["user"], password=st["password"],
                  database=st["database"], charset="utf8mb4")
    if st["port"]:
        kwargs["port"] = st["port"]

    if backend == "postgresql":
        kwargs.pop("charset", None)
        kwargs["sslmode"] = st["sslmode"]
        try:
            raw = driver.connect(connect_timeout=10, **kwargs)
        except Exception as exc:
            raise StorageError("连接 PostgreSQL 失败：%s" % exc)
        raw.set_session(autocommit=True)
    else:
        try:
            raw = driver.connect(connect_timeout=10, autocommit=True, **kwargs)
        except Exception as exc:
            raise StorageError("连接 MySQL 失败：%s" % exc)

    return _Connection(raw, backend, driver, st["database"]), backend


def disconnect():
    """关闭当前线程连接（切换配置后重启进程即可，一般无需调用）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
