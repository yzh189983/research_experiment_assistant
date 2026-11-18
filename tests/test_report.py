"""报告导出单元测试：Markdown / Word / 过程摘要。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

from core import report  # noqa: E402


def _report():
    return {
        "id": 1, "title": "测试报告", "innovation": "验证报告完整性",
        "status": "done", "created_at": 1700000000, "mode": "copilot",
        "plan": {"core_claim": "测试命题", "hypothesis": ["H1"],
                 "success_criteria": "acc > 0.9", "failure_criteria": "acc < 0.7"},
        "summary": {"verdict": "支持", "one_liner": "acc=0.92",
                    "metrics_stats": [{"name": "accuracy", "points": 10,
                                       "first": 0.8, "last": 0.92, "best": 0.93}],
                    "key_findings": ["准确率提升"]},
        "criteria": {"success": [{"metric": "accuracy", "op": ">=", "threshold": 0.9}],
                     "failure": []},
        "criteria_locked": True, "criteria_locked_at": 1700000000,
        "criteria_history": [{"changed_at": 1700000000, "reason": "更正笔误"}],
        "audits": [{"ts": 1700000000, "stage": "plan", "action": "生成方案",
                    "decision": "", "detail": {}}],
        "usage": {"calls": 3, "total_tokens": 1500, "cost": 0.02},
        "env": {"seed": 42, "python": "3.10.12", "host": "u@h:22",
                "pip_freeze": "numpy==1.26.0", "nvidia_smi": "Tesla T4",
                "captured_at": 1700000000},
        "runs": [], "latest": {"git_sha": "abcdef123456"},
        "verification": {"observed_claims": [{"metric": "accuracy", "value": 0.92}],
                         "unverified_numbers": [],
                         "evidence_sources": [{"metric": "accuracy", "value": 0.92,
                                               "step": 100, "lines": []}],
                         "criteria": {"checks": [{"kind": "成功", "metric": "accuracy",
                                                  "op": ">=", "threshold": 0.9,
                                                  "observed": 0.92, "status": "通过"}]},
                         "interpretive_notice": "仅为推断"},
    }


class TestReport(unittest.TestCase):

    def test_markdown_sections(self):
        md = report.markdown(_report())
        for kw in ("创新点", "实验方案", "预注册判据", "实验结果", "结果核验",
                   "复现实验配置", "成本与用量", "实验过程摘要",
                   "numpy==1.26.0", "seed", "abcdef123456"):
            self.assertIn(kw, md)

    def test_docx_is_valid_zip(self):
        raw = report.docx_bytes(_report())
        self.assertEqual(raw[:2], b"PK")
        self.assertGreater(len(raw), 1000)

    def test_process_summary_contains_action(self):
        text = report.process_summary(_report()["audits"])
        self.assertIn("生成方案", text)

    def test_db_time_fallback(self):
        self.assertEqual(report.db_time(None), "—")
        self.assertIn("2023", report.db_time(1700000000))


if __name__ == "__main__":
    unittest.main()
