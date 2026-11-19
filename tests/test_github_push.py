"""GitHub 推送功能单元测试：凭据隔离、敏感扫描、校验与本地裸仓库推送。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

import config  # noqa: E402
from core import crypto, db, gitlineage, workspace  # noqa: E402


class TestGitRemoteDb(unittest.TestCase):
    UID_A, UID_B = 9101, 9102

    def setUp(self):
        db.ex("DELETE FROM git_remotes WHERE user_id IN (?,?)", (self.UID_A, self.UID_B))

    def tearDown(self):
        db.ex("DELETE FROM git_remotes WHERE user_id IN (?,?)", (self.UID_A, self.UID_B))

    def test_encrypted_token_and_isolation(self):
        enc = crypto.encrypt("github_pat_secret_1234")
        rid = db.add_git_remote("alice/repo", enc, "1234", uid=self.UID_A)
        row = db.get_git_remote(rid, uid=self.UID_A)
        self.assertIsNotNone(row)
        self.assertNotIn("github_pat_secret", row["token_enc"])
        self.assertEqual(crypto.decrypt(row["token_enc"]), "github_pat_secret_1234")
        self.assertEqual(len(db.list_git_remotes(self.UID_A)), 1)
        self.assertEqual(db.list_git_remotes(self.UID_B), [])
        self.assertIsNone(db.get_git_remote(rid, uid=self.UID_B))
        db.delete_git_remote(rid, uid=self.UID_B)
        self.assertIsNotNone(db.get_git_remote(rid, uid=self.UID_A))
        db.delete_git_remote(rid, uid=self.UID_A)
        self.assertIsNone(db.get_git_remote(rid, uid=self.UID_A))

    def test_runs_push_columns_exist(self):
        cols = {r["name"] for r in db.q("PRAGMA table_info(runs)")}
        self.assertIn("remote_url", cols)
        self.assertIn("remote_sha", cols)


class TestGitlineageValidation(unittest.TestCase):

    def test_repo_and_branch_validation(self):
        self.assertEqual(gitlineage.validate_repo_full_name("owner/repo-1"), "owner/repo-1")
        for bad in ("owner", "a/b/c", "a b/c", "../x"):
            with self.assertRaises(ValueError):
                gitlineage.validate_repo_full_name(bad)
        self.assertEqual(gitlineage.validate_branch("main"), "main")
        for bad in ("-bad", "a b", "a/../b"):
            with self.assertRaises(ValueError):
                gitlineage.validate_branch(bad)

    def test_scan_sensitive(self):
        files = [
            {"path": "main.py", "content": "key = 'sk-abcdefghijklmnop'"},
            {"path": "cfg.py", "content": "PASSWORD = 'hunter2'"},
            {"path": "clean.py", "content": "print('hello')"},
        ]
        warns = gitlineage.scan_sensitive(files)
        kinds = {(w["path"], w["kind"]) for w in warns}
        self.assertIn(("main.py", "API Key"), kinds)
        self.assertIn(("cfg.py", "密码"), kinds)
        self.assertFalse(any(w["path"] == "clean.py" for w in warns))

    def test_remote_commit_url(self):
        url = gitlineage.remote_commit_url(
            "owner/repo", "a" * 40)
        self.assertEqual(url, "https://github.com/owner/repo/commit/" + "a" * 40)


@unittest.skipUnless(gitlineage.available(), "git 未安装")
class TestPushToLocalBareRepo(unittest.TestCase):
    """用本地裸仓库模拟 GitHub，验证 push 全链路，不依赖网络。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rekit-gh-push-")
        self.bare = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "--bare", "-q", self.bare], check=True)
        self.eid = 98001
        workspace.cleanup(self.eid)
        d = workspace.exp_dir(self.eid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")
        gitlineage.commit_snapshot(self.eid, {"files": [{"path": "main.py"}]},
                                   message="test")

    def tearDown(self):
        workspace.cleanup(self.eid)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_snapshot_and_cleanup(self):
        # push_snapshot 固定使用 github.com 的 HTTPS URL，这里直接测底层 git push 到裸仓库，
        # 验证 remote 设置/推送/commit 读取的组合正确。
        d = workspace.exp_dir(self.eid)
        gitlineage._git(d, "remote", "remove", "origin", check=False)
        gitlineage._git(d, "remote", "add", "origin", self.bare)
        gitlineage._git(d, "push", "-q", "origin", "HEAD:refs/heads/main",
                        no_credential=True)
        head = gitlineage._git(self.bare, "rev-parse", "refs/heads/main")
        self.assertEqual(head, gitlineage._git(d, "rev-parse", "HEAD"))
        gitlineage._git(d, "remote", "remove", "origin", check=False)


if __name__ == "__main__":
    unittest.main()
