"""API 集成测试：鉴权、越权隔离、实验链路与导出端点。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()

import app as app_module  # noqa: E402
from core import auth, db  # noqa: E402


class TestApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()
        # 建立两个用户：u1 拥有实验，u2 尝试越权
        cls.u1, cls.u2 = 9101, 9102
        for uid, name in ((cls.u1, "apitest_u1"), (cls.u2, "apitest_u2")):
            db.ex("DELETE FROM users WHERE id=?", (uid,))
            db.ex("INSERT INTO users(id,username,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                  (uid, name, "x", "member", db.now()))
        # 重试接口需要主机归属校验，给 u1 建一台测试主机
        db.ex("DELETE FROM hosts WHERE user_id=?", (cls.u1,))
        cls.host_id = db.add_host(
            {"name": "测试机", "host": "127.0.0.1", "username": "root"},
            uid=cls.u1)

    @classmethod
    def tearDownClass(cls):
        for uid in (cls.u1, cls.u2):
            db.ex("DELETE FROM experiments WHERE user_id=?", (uid,))
            db.ex("DELETE FROM hosts WHERE user_id=?", (uid,))
            db.ex("DELETE FROM users WHERE id=?", (uid,))

    def _login(self, uid):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["uid"] = uid

    def _logout(self):
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_unauthenticated_api_blocked(self):
        self._logout()
        r = self.client.get("/api/experiments")
        self.assertEqual(r.status_code, 401)

    def test_templates_and_usage(self):
        self._login(self.u1)
        self.assertEqual(self.client.get("/api/templates").status_code, 200)
        self.assertEqual(self.client.get("/api/usage").status_code, 200)

    def test_create_with_template_and_batch(self):
        self._login(self.u1)
        r = self.client.post("/api/experiments", json={
            "title": "API测试", "innovation": "验证模板字段正确写入数据库",
            "template_id": "grid"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        eid = r.get_json()["id"]
        exp = db.get_experiment(eid, uid=self.u1)
        self.assertEqual(exp["template_id"], "grid")

        r = self.client.post("/api/experiments/batch", json={
            "template_id": "grid", "innovation": "验证参数矩阵批量创建",
            "params": {"lr": [0.1, 0.01]}, "title_prefix": "API批量"})
        self.assertEqual(r.status_code, 200, r.data[:200])
        self.assertEqual(len(r.get_json()["ids"]), 2)

        for x in [eid] + r.get_json()["ids"]:
            db.ex("DELETE FROM experiments WHERE id=?", (x,))

    def test_cross_user_isolation(self):
        self._login(self.u1)
        r = self.client.post("/api/experiments", json={
            "title": "私有实验", "innovation": "这是只有用户一可见的创新点描述"})
        eid = r.get_json()["id"]
        self._login(self.u2)
        self.assertEqual(self.client.get(f"/api/experiments/{eid}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/experiments/{eid}/report.md").status_code, 404)
        self._login(self.u1)
        db.ex("DELETE FROM experiments WHERE id=?", (eid,))

    def test_mode_and_criteria_lock(self):
        self._login(self.u1)
        eid = self.client.post("/api/experiments", json={
            "title": "模式测试", "innovation": "验证副驾驶与判据锁定"}).get_json()["id"]
        r = self.client.post(f"/api/experiments/{eid}/mode", json={"mode": "copilot"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["checkpoint"], "plan")
        # 首次保存并锁定
        r = self.client.post(f"/api/experiments/{eid}/criteria", json={
            "criteria": {"success": [{"metric": "acc", "op": ">=", "threshold": 0.8}],
                         "failure": []},
            "lock": True})
        self.assertEqual(r.status_code, 200)
        exp = db.get_experiment(eid, uid=self.u1)
        self.assertTrue(exp["criteria_locked"])
        # 锁定后修改必须写原因，且原因进入历史
        r = self.client.post(f"/api/experiments/{eid}/criteria", json={
            "criteria": {"success": [{"metric": "acc", "op": ">=", "threshold": 0.85}],
                         "failure": []},
            "reason": "预注册后更正笔误"})
        self.assertEqual(r.status_code, 200)
        exp = db.get_experiment(eid, uid=self.u1)
        self.assertIn("更正笔误", exp["criteria_history_json"])
        # 锁定后无原因的修改应被拒绝
        r = self.client.post(f"/api/experiments/{eid}/criteria", json={
            "criteria": {"success": [{"metric": "acc", "op": ">=", "threshold": 0.9}],
                         "failure": []}})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(db.list_audits(eid, uid=self.u1))
        db.ex("DELETE FROM experiments WHERE id=?", (eid,))

    def test_report_endpoints(self):
        self._login(self.u1)
        eid = self.client.post("/api/experiments", json={
            "title": "报告测试", "innovation": "验证三种报告导出端点是否可用"}).get_json()["id"]
        for path, code in (
            (f"/api/experiments/{eid}/report.md", 200),
            (f"/api/experiments/{eid}/report.docx", 200),
            (f"/api/experiments/{eid}/report/print", 200),
            (f"/api/experiments/{eid}/audits", 200),
            (f"/report/{eid}", 200),
        ):
            self.assertEqual(self.client.get(path).status_code, code, path)
        db.ex("DELETE FROM experiments WHERE id=?", (eid,))

    def test_sse_event_replay(self):
        self._login(self.u1)
        eid = self.client.post("/api/experiments", json={
            "title": "事件流测试", "innovation": "验证 SSE 事件日志补发机制"}).get_json()["id"]
        i1 = db.add_stream_event(eid, "log", {"line": "first"}, uid=self.u1)
        db.add_stream_event(eid, "log", {"line": "second"}, uid=self.u1)
        # 校验落库事件可按游标补发（SSE 生成器据此工作）
        rows = db.stream_events_after(eid, i1, uid=self.u1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["payload"])["line"], "second")
        # 端点存在且返回 event-stream 类型（用 buffered 生成器，取首帧后关闭）
        r = self.client.get(f"/api/experiments/{eid}/stream",
                            headers={"Last-Event-ID": str(i1)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/event-stream", r.headers.get("Content-Type", ""))
        db.ex("DELETE FROM stream_events WHERE experiment_id=?", (eid,))
        db.ex("DELETE FROM experiments WHERE id=?", (eid,))

    def test_batch_compare_and_retry(self):
        self._login(self.u1)
        ids = [self.client.post("/api/experiments", json={
            "title": f"对比{i}", "innovation": "验证批量实验对比功能正确性",
            "template_id": "grid"}).get_json()["id"] for i in range(2)]
        r = self.client.get("/api/batch/compare?ids=" + ",".join(map(str, ids)))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["rows"]), 2)
        # 无失败 run 时重试应跳过
        r = self.client.post("/api/batch/retry-failed", json={"ids": ids, "host_id": self.host_id})
        self.assertEqual(r.status_code, 200, r.data[:200])
        for x in ids:
            db.ex("DELETE FROM experiments WHERE id=?", (x,))


if __name__ == "__main__":
    unittest.main()
