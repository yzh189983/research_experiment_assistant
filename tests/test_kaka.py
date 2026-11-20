"""卡卡智能体测试：工作台 / 群聊播报 / 数据集收集 / 模型配置。"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env
bootstrap_test_env()

import config  # noqa: E402
from core import crypto, dataset_search, db, kaka, venue_rank  # noqa: E402


class TestKaka(unittest.TestCase):
    def setUp(self):
        self.uid = 1
        self.pid = db.create_project("卡卡测试项目", "用于测试", uid=self.uid)
        self.eid = db.create_experiment(
            "卡卡测试实验", "医学 VQA 幻觉检测", uid=self.uid, project_id=self.pid)

    def tearDown(self):
        db.ex("DELETE FROM runs WHERE experiment_id=?", (self.eid,))
        db.ex("DELETE FROM ai_outputs WHERE experiment_id=?", (self.eid,))
        db.ex("DELETE FROM experiments WHERE id=?", (self.eid,))
        db.delete_project(self.pid)

    def test_workspace_stage_and_actions(self):
        ws = kaka.workspace(self.eid, uid=self.uid)
        self.assertEqual(ws["stage"], "idea")
        keys = [a["key"] for a in ws["next_actions"]]
        self.assertIn("plan", keys)
        self.assertIn("code", keys)
        self.assertIn("卡卡", ws["monitor"]["message"] or "卡卡")

    def test_ask_without_evidence_is_honest(self):
        r = kaka.ask(self.uid, "这次为什么失败？", eid=self.eid)
        self.assertIn("证据不足", r["answer"])
        self.assertEqual(r["tier"], "unverified")

    def test_broadcast_whitelist_and_messages(self):
        mid = kaka.broadcast(self.pid, "run_finished", "测试实验", "run #1 finished",
                             exp_id=self.eid, uid=self.uid)
        self.assertTrue(mid)
        self.assertIsNone(kaka.broadcast(self.pid, "unknown_event", "x", uid=self.uid))
        msgs = db.list_project_messages(self.pid, uid=self.uid)
        self.assertTrue(any(m["kind"] == "kaka" for m in msgs))
        self.assertTrue(db.add_project_message(self.pid, "user", "大家好", uid=self.uid))

    def test_collect_and_adopt_datasets(self):
        r = kaka.collect_datasets(self.pid, "医学 VQA 幻觉检测，使用 MIMIC-CXR 数据集", uid=self.uid)
        self.assertTrue(r["ok"])
        names = [c["name"] for c in r["candidates"]]
        self.assertIn("MIMIC-CXR", names)
        cand = next(c for c in r["candidates"] if c["name"] == "MIMIC-CXR")
        self.assertIn("physionet.org", cand["url"])
        self.assertEqual(cand["url_type"], "official")
        out = kaka.adopt_datasets(self.pid, [cand], uid=self.uid)
        self.assertEqual(len(out["ids"]), 1)
        rows = db.list_project_datasets(self.pid, uid=self.uid)
        self.assertTrue(any(d["name"] == "MIMIC-CXR" for d in rows))

    def test_collect_denied_without_role(self):
        r = kaka.collect_datasets(self.pid, "ImageNet", uid=99999)
        self.assertIn("error", r)

    def test_model_config_follows_main_llm_when_blank(self):
        fake = {
            "llm": {"base_url": "https://api.example.com/v1", "api_key": "main-key",
                    "model": "main-model"},
            "kaka": {"enabled": True, "model": "", "base_url": "", "api_key": "",
                     "temperature": 0.2, "max_tokens": 1200},
        }
        with mock.patch.object(config, "load_config", return_value=fake):
            c = kaka._llm_cfg()
            self.assertEqual(c["model"], "main-model")
            self.assertEqual(c["base_url"], "https://api.example.com/v1")
            self.assertEqual(c["api_key"], "main-key")
            self.assertTrue(kaka._llm_ready())

    def test_model_config_can_be_disabled(self):
        fake = {
            "llm": {"base_url": "x", "api_key": "k", "model": "m"},
            "kaka": {"enabled": False, "model": "kaka-model", "base_url": "",
                     "api_key": "", "temperature": 0.2, "max_tokens": 1200},
        }
        with mock.patch.object(config, "load_config", return_value=fake):
            self.assertFalse(kaka._llm_ready())
            self.assertEqual(kaka._llm_cfg()["model"], "kaka-model")

    def test_ask_prefers_model_when_available(self):
        with mock.patch.object(kaka, "_llm_ready", return_value=True), \
             mock.patch.object(kaka, "_llm_chat", return_value="模型说：先看诊断再重试。"):
            r = kaka.ask(self.uid, "下一步？", eid=self.eid)
            self.assertEqual(r["answer"], "模型说：先看诊断再重试。")
            self.assertEqual(r["source"], "llm")
            self.assertIn("tier", r)

    def test_global_ask_answers_any_question_with_model(self):
        with mock.patch.object(kaka, "_llm_ready", return_value=True), \
             mock.patch.object(kaka, "_llm_chat", return_value="当然可以，我们聊聊 p 值。") as m:
            r = kaka.ask_global(self.uid, "什么是 p 值？")
            self.assertEqual(r["source"], "llm")
            self.assertEqual(r["answer"], "当然可以，我们聊聊 p 值。")
            self.assertIn("任何问题", m.call_args.kwargs.get("system", ""))

    def test_external_search_parses_sources(self):
        class Resp:
            def __init__(self, data):
                self._data, self.content, self.text = data, b"", ""
            def raise_for_status(self):
                pass
            def json(self):
                return self._data

        def fake_get(url, params=None, timeout=None, headers=None):
            if "openalex" in url:
                return Resp({"results": [{
                    "title": "Chest X-Ray Study",
                    "authorships": [{"author": {"display_name": "A"}}],
                    "publication_year": 2024, "doi": "https://doi.org/10.1/x",
                    "abstract_inverted_index": {"We": [0], "used": [1], "MIMIC-CXR": [2]},
                    "primary_location": {"source": {"display_name": "CVPR"}},
                }]})
            if "huggingface.co/api/datasets/" in url:
                return Resp({"siblings": [{"rfilename": "data/train.parquet"},
                                          {"rfilename": "README.md"}]})
            if "huggingface" in url:
                return Resp([{"id": "org/mimic-cxr", "cardData": {"license": "mit"}}])
            return Resp({})

        with mock.patch.object(dataset_search.requests, "get", side_effect=fake_get):
            papers = dataset_search.search_papers("chest x-ray")
            self.assertEqual(papers[0]["title"], "Chest X-Ray Study")
            self.assertIn("MIMIC-CXR", papers[0]["abstract"])
            self.assertEqual(papers[0]["venue_tier"], "CCF-A")
            links = dataset_search.search_dataset_links("MIMIC-CXR")
            self.assertEqual(links[0]["url_type"], "huggingface")
            self.assertIn("resolve/main/data/train.parquet", links[0]["downloads"][0]["url"])

    def test_paper_search_respects_year_range(self):
        seen = {}

        class Resp:
            def __init__(self, data):
                self._data, self.content, self.text = data, b"", ""
            def raise_for_status(self):
                pass
            def json(self):
                return self._data

        def fake_get(url, params=None, timeout=None, headers=None):
            if "openalex" in url:
                seen["filter"] = (params or {}).get("filter")
                return Resp({"results": []})
            return Resp({})

        with mock.patch.object(dataset_search.requests, "get", side_effect=fake_get):
            dataset_search.search_papers("x", year_from=2020, year_to=2023)
        self.assertIn("from_publication_date:2020-01-01", seen["filter"])
        self.assertIn("to_publication_date:2023-12-31", seen["filter"])

    def test_venue_rank(self):
        self.assertEqual(venue_rank.rank_venue("CVPR"), "CCF-A")
        self.assertEqual(venue_rank.rank_venue("IEEE TPAMI"), "CCF-A / 中科院一区")
        self.assertEqual(venue_rank.rank_venue("arXiv preprint"), "")

    def test_collect_datasets_uses_external_papers(self):
        papers = [{
            "title": "Paper on MIMIC-CXR", "source": "OpenAlex", "year": 2024,
            "url": "https://doi.org/10.1/x", "authors": "A", "cited_by": 1,
            "abstract": "We used MIMIC-CXR for chest x-ray classification.",
        }]
        links = [{
            "name": "mimic-cxr", "url": "https://huggingface.co/datasets/org/mimic-cxr",
            "url_type": "huggingface", "license": "mit", "source": "Hugging Face",
        }]
        with mock.patch.object(dataset_search, "search_papers", return_value=papers), \
             mock.patch.object(dataset_search, "search_dataset_links", return_value=links), \
             mock.patch.object(kaka, "_llm_ready", return_value=False):
            r = kaka.collect_datasets(self.pid, "医学胸片分类", uid=self.uid)
            self.assertEqual(r["papers"][0]["title"], "Paper on MIMIC-CXR")
            names = [c["name"] for c in r["candidates"]]
            self.assertIn("MIMIC-CXR", names)
            cand = next(c for c in r["candidates"] if c["name"] == "MIMIC-CXR")
            self.assertTrue(cand["url"].startswith("https://huggingface.co/datasets/"))

    def test_chat_keeps_history_and_context(self):
        seen = []

        def fake_chat(messages, system=None, json_mode=False):
            seen.append([m["content"] for m in messages])
            return f"第 {len(seen)} 次回答"

        kaka.clear_history(self.uid, "global")
        with mock.patch.object(kaka, "_llm_ready", return_value=True), \
             mock.patch.object(kaka, "_llm_chat", side_effect=fake_chat):
            r1 = kaka.chat(self.uid, "第一个问题", scope="global")
            r2 = kaka.chat(self.uid, "第二个问题", scope="global")
        self.assertEqual(r1["source"], "llm")
        self.assertEqual(r2["source"], "llm")
        h = kaka.history(self.uid, "global")
        self.assertEqual([m["role"] for m in h], ["user", "assistant", "user", "assistant"])
        self.assertEqual(h[0]["content"], "第一个问题")
        self.assertEqual(h[2]["content"], "第二个问题")
        self.assertTrue(any("第一个问题" in c for c in seen[1]))
        kaka.clear_history(self.uid, "global")
        self.assertEqual(kaka.history(self.uid, "global"), [])

    def test_chat_scope_exp(self):
        kaka.clear_history(self.uid, f"exp:{self.eid}")
        with mock.patch.object(kaka, "_llm_ready", return_value=False):
            r = kaka.chat(self.uid, "下一步？", scope=f"exp:{self.eid}")
        self.assertIn("answer", r)
        h = kaka.history(self.uid, f"exp:{self.eid}")
        self.assertEqual([m["role"] for m in h], ["user", "assistant"])
        kaka.clear_history(self.uid, f"exp:{self.eid}")

    def test_kaka_api_key_is_encrypted_at_rest(self):
        uid = 9902
        try:
            config.save_user_config(uid, {"kaka": {"model": "kaka-test",
                                                   "api_key": "secret-test-key"}})
            loaded = config.load_config(uid)
            self.assertEqual(loaded["kaka"]["model"], "kaka-test")
            self.assertEqual(loaded["kaka"]["api_key"], "secret-test-key")
            with open(config.user_config_path(uid), encoding="utf-8") as f:
                raw = json.load(f)
            self.assertTrue(raw["kaka"]["api_key"].startswith(crypto.PREFIX))
        finally:
            path = config.user_config_path(uid)
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
