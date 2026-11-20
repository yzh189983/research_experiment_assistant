"""AI P0 测试：可信分级、评测回归、工具沙箱。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env
bootstrap_test_env()

from core import ai_eval, ai_tools, ai_trust, db  # noqa: E402


class TestAIP0(unittest.TestCase):
    def setUp(self):
        self.uid = 9911
        db.ex("DELETE FROM ai_outputs WHERE user_id=?", (self.uid,))
        db.ex("DELETE FROM ai_eval_runs WHERE user_id=?", (self.uid,))
        db.ex("DELETE FROM ai_eval_cases WHERE user_id=?", (self.uid,))
        db.ex("DELETE FROM ai_tool_audit WHERE user_id=?", (self.uid,))

    def tearDown(self):
        self.setUp()

    def test_ai_trust_record_and_list(self):
        evidence = {"metrics": [{"metric": "accuracy", "value": 0.82, "step": 10}],
                    "unverified_numbers": []}
        oid = ai_trust.record_output(1, 0, "plan", {"title": "测试方案"}, evidence, uid=self.uid)
        self.assertIsNotNone(oid)
        rows = db.list_ai_outputs(experiment_id=1, uid=self.uid)
        self.assertTrue(rows)
        self.assertIn(rows[0]["tier"], ("verified", "supported", "inferred", "external", "unverified"))
        self.assertIn("claims", db.q1("SELECT content_json FROM ai_outputs WHERE id=?", (oid,))["content_json"])

    def test_ai_classify_conservative(self):
        self.assertEqual(ai_trust.classify("建议增加随机种子，可能更稳健。"), "inferred")
        self.assertEqual(ai_trust.classify("文献中常见做法是交叉验证。"), "external")
        self.assertEqual(ai_trust.classify("方法有效。", {"metrics": [{"metric": "acc"}]}), "verified")

    def test_ai_eval_regression_runs(self):
        result = ai_eval.run(uid=self.uid)
        self.assertEqual(result["total"], 5)
        self.assertEqual(result["passed"], 5)
        runs = db.list_ai_eval_runs(uid=self.uid, limit=5)
        self.assertTrue(runs)
        self.assertEqual(runs[0]["model"], "local-rules-v1")

    def test_ai_tool_sandbox(self):
        names = [t["name"] for t in ai_tools.describe()]
        self.assertIn("list_projects", names)
        self.assertIn("move_experiment", names)

        denied = ai_tools.call(self.uid, "rm_rf", {"path": "/"}, confirm=True)
        self.assertFalse(denied["ok"])

        preview = ai_tools.call(self.uid, "move_experiment",
                                {"experiment_id": 1, "project_id": 1}, confirm=False)
        self.assertTrue(preview.get("needs_confirm"))

        listed = ai_tools.call(self.uid, "list_projects", {}, confirm=False)
        self.assertTrue(listed["ok"])
        self.assertTrue(db.list_ai_tool_audit(uid=self.uid))


if __name__ == "__main__":
    unittest.main()
