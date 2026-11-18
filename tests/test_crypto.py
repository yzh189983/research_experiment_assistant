"""凭据加密与脱敏的单元测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

from core import crypto  # noqa: E402


class TestCrypto(unittest.TestCase):

    def test_encrypt_decrypt_roundtrip(self):
        for value in ("sk-abcdefghijklmnop", "root", "中文密码123", "a"):
            cipher = crypto.encrypt(value)
            self.assertTrue(cipher.startswith(crypto.PREFIX), value)
            self.assertEqual(crypto.decrypt(cipher), value)

    def test_encrypt_is_idempotent(self):
        once = crypto.encrypt("secret-value")
        twice = crypto.encrypt(once)
        self.assertEqual(once, twice)

    def test_empty_passthrough(self):
        self.assertEqual(crypto.encrypt(""), "")
        self.assertIsNone(crypto.encrypt(None))
        self.assertEqual(crypto.decrypt(""), "")

    def test_decrypt_plain_passthrough(self):
        self.assertEqual(crypto.decrypt("plain-text"), "plain-text")

    def test_redact_known_secret(self):
        secret = "sk-should-be-hidden-123456"
        crypto.remember_secret(secret)
        out = crypto.redact(f"调用失败 key={secret} 结束")
        self.assertNotIn(secret, out)
        self.assertIn("结束", out)

    def test_redact_common_patterns(self):
        out = crypto.redact("Bearer sk-1234567890abcdefgh 和 password=123456")
        self.assertNotIn("sk-1234567890abcdefgh", out)


if __name__ == "__main__":
    unittest.main()
