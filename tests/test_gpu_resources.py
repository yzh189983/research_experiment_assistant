"""59 项目 GPU 排队日程表与资源管理测试。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

import app as app_module  # noqa: E402
from core import db, kaka  # noqa: E402


class TestGpuResources(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()
        cls.u1, cls.u2 = 9201, 9202
        for uid, name in ((cls.u1, "gputest_u1"), (cls.u2, "gputest_u2")):
            db.ex("DELETE FROM users WHERE id=?", (uid,))
            db.ex("INSERT INTO users(id,username,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                  (uid, name, "x", "member", db.now()))
        db.ex("DELETE FROM hosts WHERE user_id=?", (cls.u1,))
        cls.host_id = db.add_host(
            {"name": "GPU测试机", "host": "127.0.0.1", "username": "root"}, uid=cls.u1)
        cls.pid = db.create_project("GPU排程测试", uid=cls.u1)
        cls.eid = db.create_experiment("GPU排程实验", "验证 GPU 排程与提醒",
                                       uid=cls.u1, project_id=cls.pid)

    @classmethod
    def tearDownClass(cls):
        try:
            db.delete_project(cls.pid, uid=cls.u1)
        except Exception:
            pass
        db.ex("DELETE FROM experiments WHERE user_id=?", (cls.u1,))
        db.ex("DELETE FROM runs WHERE user_id=?", (cls.u1,))
        db.ex("DELETE FROM hosts WHERE user_id=?", (cls.u1,))
        db.ex("DELETE FROM users WHERE id=?", (cls.u1,))
        db.ex("DELETE FROM users WHERE id=?", (cls.u2,))

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["uid"] = uid

    def test_01_resources_schedule_and_permissions(self):
        self._login(self.u1)
        base = f"/api/projects/{self.pid}/gpu"
        r = self.client.post(base + "/resources", json={
            "host_id": self.host_id, "gpu_index": 0, "name": "A100-0",
            "memory_mb": 40960, "note": "测试卡"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        rid = r.get_json()["id"]

        r = self.client.get(base + "/resources")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(len(data["idle"]), 1)

        r = self.client.post(base + "/resources", json={
            "host_id": self.host_id, "gpu_index": 0, "name": "重复"})
        self.assertEqual(r.status_code, 400)

        t = db.now()
        r = self.client.post(base + "/schedule", json={
            "resource_id": rid, "start_at": t + 3600, "end_at": t + 7200,
            "experiment_id": self.eid, "note": "排队测试"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        sid = r.get_json()["id"]

        r = self.client.post(base + "/schedule", json={
            "resource_id": rid, "start_at": t + 5400, "end_at": t + 9000})
        self.assertEqual(r.status_code, 409)

        r = self.client.get(base + "/resources")
        self.assertEqual(len(r.get_json()["idle"]), 0)

        r = self.client.delete(base + f"/schedule/{sid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(base + "/resources")
        self.assertEqual(len(r.get_json()["idle"]), 1)

        self._login(self.u2)
        self.assertEqual(self.client.get(base + "/resources").status_code, 403)
        self.assertEqual(self.client.post(base + "/resources", json={}).status_code, 403)

    def test_02_run_bind_release_and_group_reminder(self):
        rid = db.add_project_gpu_resource(self.pid, self.host_id, 1,
                                          "A100-1", 40960, "", uid=self.u1)
        self.assertTrue(rid)
        t = db.now()
        sid = db.add_project_gpu_reservation(self.pid, rid, t - 100, t + 3600,
                                             self.eid, "", uid=self.u1)
        self.assertTrue(sid)
        run_id = db.ex("INSERT INTO runs(user_id,experiment_id,host_id,status) "
                       "VALUES(?,?,?,'pending')", (self.u1, self.eid, self.host_id))
        self.assertTrue(run_id)

        bound = db.bind_gpu_reservation_for_run(run_id, self.eid, uid=self.u1)
        self.assertIsNotNone(bound)
        row = db.q1("SELECT status, run_id FROM project_gpu_reservations WHERE id=?", (sid,))
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["run_id"], run_id)

        released = db.release_gpu_reservation_for_run(run_id)
        self.assertIsNotNone(released)
        self.assertEqual(released["project_id"], self.pid)
        row = db.q1("SELECT status, actual_end_at FROM project_gpu_reservations WHERE id=?", (sid,))
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["actual_end_at"])

        self.assertIn("gpu_idle", kaka.BROADCAST_EVENTS)
        sent = kaka.notify_idle_gpu(self.pid, uid=self.u1)
        self.assertGreaterEqual(sent, 1)
        msgs = db.list_project_messages(self.pid, uid=self.u1, limit=50)
        self.assertTrue(any("GPU 空闲" in (m.get("content") or "") for m in msgs))


if __name__ == "__main__":
    unittest.main()
