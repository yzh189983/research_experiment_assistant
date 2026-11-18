"""结果核验模块单元测试：数字回查、判据判定、证据来源。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

from core import verify  # noqa: E402


def _stats():
    return [
        {"name": "accuracy", "first": 0.80, "last": 0.92, "best": 0.93,
         "last_step": 100, "higher_better": True},
        {"name": "loss", "first": 1.20, "last": 0.15, "best": 0.14,
         "last_step": 100, "higher_better": False},
    ]


class TestVerifyNumbers(unittest.TestCase):

    def test_known_number_not_flagged(self):
        summary = {"verdict": "支持", "one_liner": "准确率 0.92", "key_findings": ["loss 0.15"]}
        self.assertEqual(verify.verify_numbers(summary, _stats()), [])

    def test_unknown_number_flagged(self):
        summary = {"one_liner": "准确率达到 0.97"}
        bad = verify.verify_numbers(summary, _stats())
        self.assertTrue(bad)
        self.assertIn("0.97", str(bad))

    def test_no_numbers_clean(self):
        self.assertEqual(verify.verify_numbers({"one_liner": "效果显著"}, _stats()), [])


class TestCheckCriteria(unittest.TestCase):

    def test_ge_pass_and_fail(self):
        criteria = {"success": [{"metric": "accuracy", "op": ">=", "threshold": 0.9}], "failure": []}
        passed = verify.check_criteria(criteria, True, {}, _stats())
        self.assertEqual(passed["checks"][0]["status"], "通过")
        criteria["success"][0]["threshold"] = 0.99
        failed = verify.check_criteria(criteria, True, {}, _stats())
        self.assertNotEqual(failed["checks"][0]["status"], "通过")

    def test_best_vs_last(self):
        criteria = {"success": [{"metric": "accuracy", "op": ">=",
                                 "threshold": 0.925, "use": "best"}], "failure": []}
        res = verify.check_criteria(criteria, True, {}, _stats())
        self.assertEqual(res["checks"][0]["status"], "通过")
        criteria["success"][0]["use"] = "last"
        res2 = verify.check_criteria(criteria, True, {}, _stats())
        self.assertNotEqual(res2["checks"][0]["status"], "通过")

    def test_unknown_metric_is_unverified(self):
        criteria = {"success": [{"metric": "f1", "op": ">=", "threshold": 0.8}], "failure": []}
        res = verify.check_criteria(criteria, True, {}, _stats())
        self.assertEqual(res["checks"][0]["status"], "待确认")

    def test_unlocked_criteria_noted(self):
        criteria = {"success": [{"metric": "accuracy", "op": ">=", "threshold": 0.9}], "failure": []}
        locked = verify.check_criteria(criteria, True, {}, _stats())
        unlocked = verify.check_criteria(criteria, False, {}, _stats())
        self.assertNotEqual(locked.get("summary"), unlocked.get("summary"))


class TestEvidenceSources(unittest.TestCase):

    def test_extracts_line_numbers(self):
        log = "start\n@@METRIC name=accuracy value=0.91 step=50\n@@METRIC name=accuracy value=0.92 step=100\n"
        src = verify.evidence_sources(_stats(), log)
        self.assertTrue(src)
        self.assertEqual(src[0]["metric"], "accuracy")
        self.assertTrue(src[0]["lines"])
        self.assertIn("line", src[0]["lines"][0])

    def test_verify_summary_shape(self):
        criteria = {"success": [{"metric": "accuracy", "op": ">=", "threshold": 0.9}], "failure": []}
        result = verify.verify_summary(
            {"one_liner": "准确率 0.97"}, _stats(), "accuracy=0.92",
            criteria=criteria, criteria_locked=True, plan={})
        for key in ("evidence_sources", "unverified_numbers", "criteria",
                    "observed_claims", "interpretive_notice"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
