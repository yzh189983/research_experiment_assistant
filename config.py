"""
全局配置 —— 所有可调参数集中在这里 / data/config.json 单一文件。

优先级：用户级配置 > data/config.json（系统默认） > config.py 默认值
UI 的「设置」页面会直接改写当前用户的配置，不需要改代码。

多用户：current_uid() 用 ContextVar 承载当前请求/线程的用户身份。
Web 请求在 before_request 里设置，后台线程在入口用 as_user(uid) 包裹。
"""
import contextvars
import json
import os
import copy
import secrets

from core import crypto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
WORKSPACE_DIR = os.path.join(DATA_DIR, "workspace")   # 每个实验的本地代码目录
DB_PATH = os.path.join(DATA_DIR, "app.db")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
USERS_DIR = os.path.join(DATA_DIR, "users")           # 每用户一份配置覆盖
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")        # 用户头像（实际数据，随 data/ 被忽略）
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret.key")

DEFAULTS = {
    "llm": {
        # 任何 OpenAI 兼容接口都能用：智谱 / DeepSeek / Qwen / OpenAI
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-plus",
        "temperature": 0.35,
        "timeout": 240,
        "fallback_models": [],      # 主模型失败时的备用模型列表
        "max_retries": 3,           # 429 / 5xx / 超时 的指数退避重试次数
        "retry_base_sec": 1.0,      # 退避基数（1s, 2s, 4s…）
        "stream": False,            # 是否流式输出（前端逐字显示时开启）
        "circuit_break_after": 5,   # 连续失败 N 次熔断，避免烧 token
    },
    "kaka": {
        # 卡卡智能体专用模型；留空的字段自动跟随主 LLM，方便单独换模型而不改全局
        "enabled": True,            # 关掉则只用内置规则回答，不调用大模型
        "model": "",                # 卡卡模型名，例如 glm-4-flash / deepseek-chat
        "base_url": "",             # 卡卡接口地址，留空跟随 llm.base_url
        "api_key": "",              # 卡卡专用 Key，留空跟随 llm.api_key
        "temperature": 0.2,
        "max_tokens": 1200,
        "context_window": 128000,   # 上下文窗口上限，用于卡卡用量条；0 表示按模型名推断
    },
    "dataset_search": {
        # 卡卡数据集收集的外部检索；失败会自动退回内置目录，不影响主流程
        "enabled": True,
        "sources": ["openalex", "arxiv", "huggingface"],  # 可加 "zenodo"
        "timeout": 8,               # 单个来源超时秒数
        "max_papers": 5,
        "max_links": 4,
        "prefer_top_venues": True,  # 优先 CCF-A / 中科院一区论文
        "year_from": 0,             # 论文起始年份；0 = 不限
        "year_to": 0,               # 论文结束年份；0 = 不限
    },
    "monitor": {
        "interval_sec": 3,          # 轮询间隔
        "max_metrics_points": 800,  # 单条曲线最多保留点数
        "stall_warn_sec": 600,      # 日志多久没增长就提示可能卡住
    },
    "server": {
        "host": "127.0.0.1",
        "port": 7788,
        "scheme": "https",              # 默认启用本地 HTTPS/WSS，保护 session 与终端
        "force_https": True,            # 强制 HTTP 请求跳转到 HTTPS
        "ssl_cert": "data/certs/local-cert.pem",
        "ssl_key": "data/certs/local-key.pem",
    },
    "security": {
        "allowed_origins": [],          # 额外允许的 WebSocket Origin
        "trust_proxy": False,           # 是否信任 X-Forwarded-Proto
        "redact_logs": True,
    },
    "terminal": {
        "ticket_ttl_sec": 60,           # 一次性终端票据有效期
        "idle_timeout_sec": 900,        # 15 分钟无消息自动断开
        "max_seconds": 14400,           # 4 小时硬上限
        "ping_interval_sec": 15,
    },
    "codegen": {
        # 生成代码时必须遵守的指标上报协议，监控端靠它解析曲线
        "metric_protocol": "@@METRIC name=<指标名> value=<数值> [step=<步数>]",
    },
    "auth": {
        "enabled": True,            # 登录鉴权总开关；关掉即回到单用户免登录模式
        "session_days": 14,
        "allow_registration": True, # 是否允许访客在登录页自助注册成员账号
    },
    "notify": {
        "enabled": False,           # 通知总开关
        "webhook_url": "",          # Webhook 地址（企业微信/钉钉/自定义）
        "events": ["finished", "failed", "criteria_failed"],  # 触发事件
        "quiet_hours": "",          # 免打扰时段，如 "22:00-08:00"
    },
    "storage": {
        # 数据后端：sqlite（默认，data/app.db）| postgresql | mysql
        # 留空 / sqlite 时完全沿用原来的本地 SQLite 文件存储，零行为变化。
        # 配了 postgresql/mysql 才需要对应驱动（psycopg2-binary / PyMySQL）。
        "backend": "sqlite",
        "host": "127.0.0.1",
        "port": 0,                  # 0 = 各后端默认端口（5432 / 3306）
        "database": "rekit",
        "user": "",
        "password": "",             # 明文填写即可，落盘会自动加密
        "sslmode": "disable",       # PostgreSQL 的 sslmode；MySQL 忽略
    },
}


# ---------------- 当前用户上下文 ----------------
_uid_var = contextvars.ContextVar("rekit_uid", default=None)


def current_uid():
    return _uid_var.get()


def set_current_uid(uid):
    return _uid_var.set(uid)


def reset_current_uid(token):
    _uid_var.reset(token)


class as_user:
    """with config.as_user(uid): 在该作用域内把当前用户设为 uid。

    用于后台线程入口（Flask 请求上下文不存在，也不会自动带 ContextVar）。
    """

    def __init__(self, uid):
        self.uid = uid
        self._token = None

    def __enter__(self):
        self._token = set_current_uid(self.uid)
        return self

    def __exit__(self, *exc):
        reset_current_uid(self._token)
        return False


def get_or_create_secret_key():
    """Flask session 签名密钥：首次生成后固定下来，重启不失效。"""
    ensure_dirs()
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(SECRET_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def _deep_merge(base, patch):
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_dirs():
    for d in (DATA_DIR, WORKSPACE_DIR, USERS_DIR, AVATAR_DIR):
        # 先判断存在再建：目录通常都在，避免无谓调用 os.makedirs
        # （某些沙箱/打包环境下 makedirs 即使 exist_ok 也会被拦截报错）
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)


def _read_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def user_config_path(uid):
    return os.path.join(USERS_DIR, str(uid), "config.json")


def _decode_secrets(cfg):
    for section in ("llm", "kaka"):
        key = (cfg.get(section) or {}).get("api_key")
        if key and str(key).startswith(crypto.PREFIX):
            cfg[section]["api_key"] = crypto.decrypt(key)
    storage = cfg.get("storage") or {}
    password = storage.get("password")
    if password and str(password).startswith(crypto.PREFIX):
        storage["password"] = crypto.decrypt(password)
    return cfg


def _encode_secrets(patch):
    out = copy.deepcopy(patch or {})
    for section in ("llm", "kaka"):
        if not isinstance(out.get(section), dict) or "api_key" not in out[section]:
            continue
        key = out[section]["api_key"]
        if key and not str(key).startswith(crypto.PREFIX) and "*" not in str(key):
            out[section]["api_key"] = crypto.encrypt(str(key))
    storage = out.get("storage")
    if isinstance(storage, dict) and storage.get("password"):
        pwd = storage["password"]
        if not str(pwd).startswith(crypto.PREFIX) and "*" not in str(pwd):
            storage["password"] = crypto.encrypt(str(pwd))
    return out


def load_global_config():
    """系统级配置（不含用户覆盖），所有用户的默认值。"""
    ensure_dirs()
    return _decode_secrets(_deep_merge(DEFAULTS, _read_json(CONFIG_FILE)))


def load_config(uid=None):
    """加载配置：系统默认 + 当前用户覆盖。

    uid=None 时取当前上下文用户；没有上下文则只读系统级配置。
    """
    cfg = load_global_config()
    if uid is None:
        uid = current_uid()
    if uid is not None:
        cfg = _deep_merge(cfg, _read_json(user_config_path(uid)))
    return _decode_secrets(cfg)


def save_config(patch):
    """写系统级配置（只在初始化/迁移时用；UI 的改动请走 save_user_config）。"""
    ensure_dirs()
    merged = _deep_merge(_read_json(CONFIG_FILE), _encode_secrets(patch))
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return load_global_config()


def save_user_config(uid, patch):
    """把用户改动写入 data/users/<uid>/config.json，只存差异部分。"""
    ensure_dirs()
    path = user_config_path(uid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    merged = _deep_merge(_read_json(path), _encode_secrets(patch))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return load_config(uid)


def migrate_config_secrets():
    """把已有明文 API Key 原地加密；重复调用安全。"""
    ensure_dirs()
    paths = [CONFIG_FILE]
    if os.path.isdir(USERS_DIR):
        for root, _, files in os.walk(USERS_DIR):
            if "config.json" in files:
                paths.append(os.path.join(root, "config.json"))
    for path in paths:
        raw = _read_json(path)
        changed = False
        for section in ("llm", "kaka"):
            key = (raw.get(section) or {}).get("api_key")
            if not key or str(key).startswith(crypto.PREFIX) or set(str(key)) == {"*"}:
                continue
            raw[section]["api_key"] = crypto.encrypt(key)
            changed = True
        storage = raw.get("storage") or {}
        if storage.get("password") and not str(storage["password"]).startswith(crypto.PREFIX):
            storage["password"] = crypto.encrypt(str(storage["password"]))
            raw["storage"] = storage
            changed = True
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)


def mask_secret(s):
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return s[:4] + "*" * (len(s) - 8) + s[-4:]
