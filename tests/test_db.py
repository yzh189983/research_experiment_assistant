"""数据层单元测试：schema、多用户隔离、事件与审计。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

import config  # noqa: E402
from core import db  # noqa: E402


class TestDb(unittest.TestCase):

    def setUp(self):
        self.uid_a, self.uid_b = 9001, 9002
        db.ex("DELETE FROM experiments WHERE user_id IN (?,?)", (self.uid_a, self.uid_b))
        db.ex("DELETE FROM users WHERE id IN (?,?)", (self.uid_a, self.uid_b))

    def tearDown(self):
        db.ex("DELETE FROM experiments WHERE user_id IN (?,?)", (self.uid_a, self.uid_b))

    def test_wal_enabled(self):
        mode = db.q1("PRAGMA journal_mode")["journal_mode"]
        self.assertEqual(str(mode).lower(), "wal")

    def test_required_tables_exist(self):
        names = {r["name"] for r in db.q(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("users", "hosts", "experiments", "runs", "metrics",
                  "events", "audit_log", "usage_log", "terminal_tickets"):
            self.assertIn(t, names)

    def test_create_and_get_experiment(self):
        eid = db.create_experiment("隔离测试", "验证实验", uid=self.uid_a)
        got = db.get_experiment(eid, uid=self.uid_a)
        self.assertEqual(got["title"], "隔离测试")
        self.assertEqual(db.get_experiment(eid, uid=self.uid_b), None)

    def test_list_experiments_isolated(self):
        ea = db.create_experiment("A的实验", "a", uid=self.uid_a)
        eb = db.create_experiment("B的实验", "b", uid=self.uid_b)
        ids_a = {e["id"] for e in db.list_experiments(uid=self.uid_a)}
        ids_b = {e["id"] for e in db.list_experiments(uid=self.uid_b)}
        self.assertIn(ea, ids_a)
        self.assertNotIn(eb, ids_a)
        self.assertIn(eb, ids_b)

    def test_events_after_cursor(self):
        eid = db.create_experiment("事件测试", "e", uid=self.uid_a)
        first = db.add_event(eid, None, "第一条", uid=self.uid_a)
        db.add_event(eid, None, "第二条", uid=self.uid_a)
        rows = db.events_after(eid, last_id=first, uid=self.uid_a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "第二条")
        self.assertEqual(db.events_after(eid, last_id=999999, uid=self.uid_a), [])

    def test_stream_event_journal(self):
        eid = db.create_experiment("事件日志测试", "s", uid=self.uid_a)
        i1 = db.add_stream_event(eid, "status", {"status": "running"}, uid=self.uid_a)
        i2 = db.add_stream_event(eid, "log", {"line": "hello"}, uid=self.uid_a)
        self.assertGreater(i2, i1)
        rows = db.stream_events_after(eid, i1, uid=self.uid_a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "log")
        self.assertEqual(db.stream_event_cursor(eid, uid=self.uid_a), i2)
        # 越权：u2 读不到 u1 的事件
        self.assertEqual(db.stream_events_after(eid, 0, uid=self.uid_b), [])
        db.prune_stream_events(eid, keep=0, uid=self.uid_a)
        self.assertEqual(db.stream_events_after(eid, 0, uid=self.uid_a), [])

    def test_heartbeat_and_stale_runs(self):
        eid = db.create_experiment("心跳测试", "h", uid=self.uid_a)
        rid = db.create_run(eid, 1, "/tmp/x", "/tmp/y", uid=self.uid_a)
        db.update_run(rid, status="running", uid=self.uid_a)
        db.touch_run(rid, uid=self.uid_a)
        self.assertIsNotNone(db.get_run(rid, uid=self.uid_a)["heartbeat_at"])
        self.assertNotIn(rid, [r["id"] for r in db.stale_running_runs(uid=self.uid_a)])
        # 人工把心跳改旧，应被判为僵尸
        old = db.now() - 1000
        db.ex("UPDATE runs SET heartbeat_at=? WHERE id=?", (old, rid))
        self.assertIn(rid, [r["id"] for r in db.stale_running_runs(uid=self.uid_a)])
        db.ex("DELETE FROM runs WHERE id=?", (rid,))

    def test_audit_and_usage_written(self):
        eid = db.create_experiment("审计测试", "a", uid=self.uid_a)
        db.add_audit(eid, "plan", "生成方案", {"n": 1}, uid=self.uid_a)
        audits = db.list_audits(eid, uid=self.uid_a)
        self.assertTrue(audits)
        self.assertEqual(audits[0]["action"], "生成方案")

        db.add_usage("plan", "test-model", 100, 50, 0.001,
                     experiment_id=eid, uid=self.uid_a)
        summary = db.usage_summary(eid, uid=self.uid_a)
        self.assertEqual(summary["calls"], 1)
        self.assertEqual(summary["total_tokens"], 150)


if __name__ == "__main__":
    unittest.main()
