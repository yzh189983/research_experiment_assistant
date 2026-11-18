"""测试环境隔离：所有测试共用临时 data 目录，绝不触碰真实的 data/。

同时兼容 pytest（通过 conftest.py）与标准库 unittest（各测试类 setUp 调用
bootstrap_test_env()）。该函数幂等，重复调用无副作用。
"""
import os
import tempfile
import threading

_DONE = False
_TMP = None
_LOCK = threading.Lock()


def bootstrap_test_env():
    global _DONE, _TMP
    with _LOCK:
        if _DONE:
            return _TMP

        os.environ["REKIT_FORCE_HTTPS"] = "0"
        os.environ.setdefault("REKIT_MASTER_PASSWORD", "rekit-test-master-password-2026")

        import config
        _TMP = tempfile.mkdtemp(prefix="rekit-test-")
        data = os.path.join(_TMP, "data")
        config.DATA_DIR = data
        config.WORKSPACE_DIR = os.path.join(data, "workspace")
        config.DB_PATH = os.path.join(data, "app.db")
        config.CONFIG_FILE = os.path.join(data, "config.json")
        config.USERS_DIR = os.path.join(data, "users")
        config.SECRET_KEY_FILE = os.path.join(data, "secret.key")
        config.ensure_dirs()

        from core import db
        # db.py 在 import 时把 DB_PATH 绑到了模块全局，这里重定向到临时库
        db.DB_PATH = config.DB_PATH
        if getattr(db._local, "conn", None) is not None:
            try:
                db._local.conn.close()
            except Exception:
                pass
            del db._local.conn
        db.init_db()

        _DONE = True
        return _TMP


def tmp_dir():
    return bootstrap_test_env()
