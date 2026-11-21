"""密码重置令牌与邮箱账号单元测试。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# 隔离数据目录
_tmp = tempfile.mkdtemp(prefix="rekit-reset-")
config.DATA_DIR = _tmp
config.CONFIG_FILE = os.path.join(_tmp, "config.json")
config.ensure_dirs()

from core import db, mail  # noqa: E402
from core import auth  # noqa: E402


class TestPasswordReset(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 直接建库（init_db 内部用 db.DB_PATH）
        db.DB_PATH = os.path.join(_tmp, "app.db")
        db.init_db()

    def setUp(self):
        # 每个测试用独立用户名
        self.uname = "u_%s" % self.id()

    def test_add_user_with_email_and_lookup(self):
        uid = db.add_user(self.uname, auth.hash_password("secret1"), email="  Test@Example.COM ")
        row = db.get_user_by_name(self.uname)
        self.assertEqual(row["email"], "test@example.com")
        by_email = db.get_user_by_email("TEST@example.com")
        self.assertEqual(by_email["id"], uid)
        self.assertIsNone(db.get_user_by_email("nobody@example.com"))
        self.assertIsNone(db.get_user_by_email(""))

    def test_token_issue_and_consume(self):
        uid = db.add_user(self.uname, auth.hash_password("secret1"), email="%s@ex.com" % self.uname)
        token, expires = db.issue_password_reset_token(uid, ttl_sec=1800)
        self.assertTrue(token)
        self.assertGreater(expires, db.now())

        # 库中只存摘要，不存明文
        row = db.q1("SELECT token_hash FROM password_reset_tokens WHERE uid=?", (uid,))
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(len(row["token_hash"]), 64)

        # 消费成功
        self.assertEqual(db.consume_password_reset_token(token), uid)
        # 一次性：再消费失败
        self.assertIsNone(db.consume_password_reset_token(token))

    def test_expired_token_rejected(self):
        uid = db.add_user(self.uname, auth.hash_password("secret1"))
        token, _ = db.issue_password_reset_token(uid, ttl_sec=60)
        # 手动改成已过期
        db.ex("UPDATE password_reset_tokens SET expires_at=? WHERE uid=?", (db.now() - 1, uid))
        self.assertIsNone(db.consume_password_reset_token(token))

    def test_new_token_invalidates_old(self):
        uid = db.add_user(self.uname, auth.hash_password("secret1"))
        old, _ = db.issue_password_reset_token(uid, ttl_sec=1800)
        new, _ = db.issue_password_reset_token(uid, ttl_sec=1800)
        self.assertIsNone(db.consume_password_reset_token(old))
        self.assertEqual(db.consume_password_reset_token(new), uid)

    def test_update_password_and_revoke_token(self):
        uid = db.add_user(self.uname, auth.hash_password("oldpass"))
        old_hash = db.get_user_by_name(self.uname)["password_hash"]
        db.update_user(uid, password_hash=auth.hash_password("newpass"), api_token_hash="", token_salt="")
        row = db.get_user_by_name(self.uname)
        self.assertNotEqual(row["password_hash"], old_hash)
        self.assertTrue(auth.verify_password(row, "newpass"))
        self.assertEqual(row["api_token_hash"], "")


class TestMailConfig(unittest.TestCase):

    def test_disabled_by_default(self):
        self.assertFalse(mail.is_enabled())

    def test_normalize_email_in_app(self):
        import app
        self.assertEqual(app._normalize_email("  A@B.COM "), "a@b.com")
        self.assertEqual(app._normalize_email(""), "")
        self.assertIsNone(app._normalize_email("bad"))
        self.assertIsNone(app._normalize_email("@b.com"))

    def test_mail_password_encrypted_at_rest(self):
        config.save_config({"mail": {"enabled": True, "host": "smtp.ex.com", "password": "s3cr3t"}})
        raw = config._read_json(config.CONFIG_FILE)
        self.assertNotEqual(raw["mail"]["password"], "s3cr3t")
        self.assertTrue(str(raw["mail"]["password"]).startswith(config.crypto.PREFIX))
        # load 时解密
        self.assertEqual(config.load_config()["mail"]["password"], "s3cr3t")


if __name__ == "__main__":
    unittest.main()
