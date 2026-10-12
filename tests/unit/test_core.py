"""核心逻辑的单元测试（只用标准库，不联网、不调用模型）。运行：python -m unittest discover -s tests/unit"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp(prefix="wb_unit_")
os.environ["WB_HOME"] = _tmp

from workbench import config, sessions, stats, util  # noqa: E402
from workbench.papers import codex, explain, ids, intake, llm, quota  # noqa: E402
from workbench.papers import field as fieldmod  # noqa: E402


class TestRedaction(unittest.TestCase):
    def test_env_keys_and_values(self):
        env = util.redact_env({"OPENAI_API_KEY": "abc", "HF_HOME": "/x", "NOTE": "token sk-" + "a" * 30,
                               "CUDA_VISIBLE_DEVICES": "0,1", "LS_COLORS": "x"})
        self.assertEqual(env["OPENAI_API_KEY"], util.REDACTED)
        self.assertEqual(env["HF_HOME"], "/x")
        self.assertNotIn("sk-", env["NOTE"])
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0,1")
        self.assertNotIn("LS_COLORS", env)

    def test_argv(self):
        out = util.redact_argv(["python", "a.py", "--api_key", "secret", "--token=xyz", "--key_file", "/k"])
        self.assertEqual(out[3], util.REDACTED)
        self.assertEqual(out[4], f"--token={util.REDACTED}")
        self.assertEqual(out[6], "/k")  # 路径类参数不脱敏


class TestIds(unittest.TestCase):
    def test_arxiv(self):
        self.assertEqual(ids.arxiv_id("https://arxiv.org/abs/2502.04180v2"), "2502.04180")
        self.assertEqual(ids.arxiv_id("10.48550/arXiv.2410.10762"), "2410.10762")
        self.assertEqual(ids.arxiv_id("2507.18224"), "2507.18224")
        self.assertIsNone(ids.arxiv_id("not an id"))

    def test_paper_id(self):
        self.assertEqual(ids.paper_id(arxiv="2502.04180"), "arxiv-2502.04180")
        self.assertEqual(ids.paper_id(doi="10.48550/arxiv.2502.04180"), "arxiv-2502.04180")
        self.assertTrue(ids.paper_id(title="Some Title").startswith("t-"))


class TestStats(unittest.TestCase):
    def test_mean_std(self):
        m, s = stats.mean_std([1.0, 2.0, 3.0])
        self.assertAlmostEqual(m, 2.0)
        self.assertAlmostEqual(s, 1.0)
        self.assertEqual(stats.mean_std([5.0]), (5.0, None))

    def test_config_key_ignores_seed_and_outputs(self):
        a = {"argv": ["/usr/bin/python3", "train.py", "--seed", "0", "--output_dir", "out/s0", "--lr", "0.1"]}
        b = {"argv": ["python3", "train.py", "--seed=1", "--output_dir=out/s1", "--lr", "0.1"]}
        c = {"argv": ["python3", "train.py", "--seed", "2", "--lr", "0.2"]}
        f = ["seed"]
        self.assertEqual(stats.config_key(a, f), stats.config_key(b, f))
        self.assertNotEqual(stats.config_key(a, f), stats.config_key(c, f))


class TestSessions(unittest.TestCase):
    def test_loop_variable_matching(self):
        txt = "for s in 0 1 2; do nohup python3 train.py --seed $s --output_dir out/seed$s > log_seed$s.txt"
        self.assertTrue(sessions._token_in("out/seed0", txt))
        self.assertTrue(sessions._token_in("train.py", txt))
        self.assertFalse(sessions._token_in("eval.py", txt))

    def test_codex_unescape(self):
        raw = 'cmd: "cd /x && nohup python run.py \\\\\\n  --k 1 > a.log"'
        self.assertIn("run.py", sessions._norm_tool(raw))
        self.assertNotIn("\\n", sessions._norm_tool(raw))


class TestQuota(unittest.TestCase):
    def test_codex_reset_time(self):
        t = codex._reset_from_log("ERROR: You've hit your usage limit ... or try again at 3:16 PM.")
        lt = time.localtime(t)
        self.assertEqual((lt.tm_hour, lt.tm_min), (15, 18))  # 3:16 PM + 2 分钟余量

    def test_llm_reset_time(self):
        t = llm._reset_time("429 rate limit exceeded, try again in 20 min")
        self.assertAlmostEqual(t - time.time(), 1200, delta=5)
        self.assertIsNone(llm._reset_time("something else"))

    def test_quota_rx(self):
        for s in ("insufficient_quota", "Rate limit reached", "今日额度已用完"):
            self.assertTrue(llm.QUOTA_RX.search(s), s)
        self.assertFalse(llm.QUOTA_RX.search("upstream_connect_failed"))

    def _snap(self):
        now = time.time()
        w = lambda r, reset=now + 3600: {"label": "x", "remaining": r, "reset_at": reset}  # noqa: E731
        return {"credentials": [
            {"id": 5, "status": "available", "cooldowns": {"gemini-x": now + 600},
             "windows": {"gemini-5h": w(0.1, now + 600), "gemini-weekly": w(82.7), "3p-5h": w(100), "3p-weekly": w(100)}},
            {"id": 13, "status": "available", "cooldowns": {},
             "windows": {"gemini-5h": w(100), "gemini-weekly": w(100), "3p-5h": w(100), "3p-weekly": w(100)}},
        ]}

    def test_pool_skips_cooled_credential(self):
        p = quota.pool(self._snap(), "gemini-x")
        self.assertEqual([c["id"] for c in p["usable"]], [13])
        self.assertEqual(p["best_5h"]["remaining"], 100)

    def test_pool_none_usable_gives_next_time(self):
        s = self._snap()
        s["credentials"][1]["cooldowns"]["gemini-x"] = time.time() + 300
        p = quota.pool(s, "gemini-x")
        self.assertEqual(p["usable"], [])
        self.assertAlmostEqual(p["next_at"] - time.time(), 300, delta=5)  # 凭据 13 先恢复

    def test_pool_backfill_threshold(self):
        s = self._snap()
        s["credentials"][1]["windows"]["gemini-5h"]["remaining"] = 30
        self.assertEqual(quota.pool(s, "gemini-x", 40, 35)["usable"], [])
        self.assertEqual(len(quota.pool(s, "gemini-x")["usable"]), 1)

    def test_block_file_stops_requests(self):
        llm._block_path().parent.mkdir(parents=True, exist_ok=True)
        llm._block_path().write_text(json.dumps({"reason": "测试", "until": time.time() + 60, "until_text": "x"}))
        try:
            with self.assertRaises(llm.LLMPaused):
                llm.check_budget("daily", "gemini-x")
        finally:
            llm._block_path().unlink()


class TestPapers(unittest.TestCase):
    def test_normalize_facets(self):
        f = json.loads((ROOT / "examples" / "fields" / "mas-arch-opt.json").read_text())
        out = intake.normalize_facets({"optimizes": ["算子", "通信拓扑"], "search": ["超网", "完全新的方法"]}, f)
        self.assertIn("workflow 代码 / 算子", out["optimizes"])
        self.assertIn("通信拓扑", out["optimizes"])
        self.assertIn("可微 / 超网", out["search"])
        self.assertTrue(any(v.startswith("其它:") for v in out["search"]))

    def test_check_html(self):
        p = Path(_tmp) / "x.html"
        p.write_text('<a href="#ref-1">[1]</a><li id="ref-1"></li>' + "x" * 9000)
        self.assertEqual(explain.check_html(p), [])
        p.write_text('{{STEP1_CONTENT}}<a href="#ref-2">[2]</a>' + "x" * 9000)
        probs = explain.check_html(p)
        self.assertTrue(any("占位符" in x for x in probs) and any("引用" in x for x in probs))

    def test_field_init_from_example(self):
        res = fieldmod.init_from("mas-arch-opt")
        self.assertEqual(res["seeds"], 5)
        self.assertEqual(fieldmod.field()["id"], "mas-arch-opt")

    def test_default_field_is_generic(self):
        self.assertEqual(fieldmod.DEFAULT_FIELD["arxiv_keywords"], [])


class TestConfigDefaults(unittest.TestCase):
    def test_no_personal_defaults(self):
        d = config.DEFAULTS
        self.assertEqual(d["watch_roots"], [])
        self.assertIsNone(d["panel_url"])
        self.assertIsNone(d["telegram_env"])


class TestManualAndProfile(unittest.TestCase):
    def setUp(self):
        from workbench.papers import store
        self.store = store
        os.environ["WB_LLM_PROTOCOL"] = "fake"
        fieldmod.init_from("mas-arch-opt")
        for pid, extra in (("arxiv-9999.00001", {"manual": {"why": "测试"}}), ("arxiv-9999.00002", {})):
            m = {"id": pid, "arxiv": pid.split("-")[1], "title": f"Paper {pid}", "abstract": "multi-agent workflow",
                 "intake": {"state": "relevant"}, "published": "2026-10-01", "sources": [], **extra}
            store.save(m)
        store.save_status("arxiv-9999.00002", {"irrelevant": "2026-10-12T10:00:00+08:00", "note": "单智能体"})
        store._cache.clear()

    def tearDown(self):
        os.environ.pop("WB_LLM_PROTOCOL", None)

    def test_manual_not_picked(self):
        from workbench.papers import select
        self.assertFalse(select.eligible(self.store.load("arxiv-9999.00001"), "2026-10-12"))

    def test_negatives_in_relevance_prompt(self):
        txt = intake._negative_titles()
        self.assertIn("Paper arxiv-9999.00002", txt)
        self.assertIn("单智能体", txt)

    def test_suggest_then_apply(self):
        from workbench.papers import manual
        before = fieldmod.field()["description"]
        prop = manual.suggest()
        self.assertEqual(prop["based_on"]["negative"], 1)
        self.assertEqual(fieldmod.field()["description"], before)  # suggest 不写入
        manual.apply()
        self.assertEqual(fieldmod.field()["description"], "（假模型）新的方向描述")
        self.assertFalse(manual.proposal_path().exists())
        self.assertTrue(list((config.home() / "registry" / "field.history").glob("field-*.json")))


class TestResultsTable(unittest.TestCase):
    def test_dataset_lookup_and_category(self):
        from workbench.papers import datasets
        self.assertEqual(datasets.lookup("gsm-8k")["name"], "GSM8K")
        self.assertEqual(datasets.lookup("GPQA-Diamond")["category"], "knowledge")
        self.assertNotEqual(datasets.lookup("AIME25")["name"], datasets.lookup("AIME 2024")["name"])
        self.assertTrue(datasets.lookup("Avg.")["aggregate"])
        self.assertTrue(datasets.lookup("SomeNewBench")["pending"])
        v = datasets.lookup("MATH (lv5*)")
        self.assertEqual((v["name"], v["variant"]), ("MATH", "lv5*"))   # 子集写法并到父数据集
        self.assertEqual(datasets.scale({"n_eval": 617}, v["full"], v["variant"])["status"], "subset")
        self.assertIsNone(datasets.lookup("GSM8K (free generation)").get("variant"))

    def test_scale(self):
        from workbench.papers import datasets
        full = datasets.lookup("MATH")["full"]
        self.assertEqual(datasets.scale({"n_eval": 617}, full)["status"], "subset")
        self.assertEqual(datasets.scale({"n_eval": 5000}, full)["status"], "full")
        self.assertEqual(datasets.scale({"sampled": False}, None)["status"], "full")
        self.assertEqual(datasets.scale(None, full)["status"], "unknown")
        # 论文把 1055 题叫"完整测试集"：按官方全量 1319 判为子集
        self.assertEqual(datasets.scale({"n_eval": 1055, "full_size": 1055, "sampled": False}, datasets.lookup("GSM8K")["full"])["status"], "subset")
        self.assertEqual(datasets.scale({"n_eval": 500, "sampled": True}, datasets.lookup("MATH-500")["full"])["status"], "full")

    def test_metric_and_method_norm(self):
        from workbench.papers import results
        self.assertEqual(results.canon_metric("Acc.", "%", "math")[:2], ("Accuracy", "quality"))
        self.assertEqual(results.canon_metric("Solve Rate", None, "code")[0], "Pass@1")
        self.assertEqual(results.canon_metric("Cost", "$", "math")[1:], ("cost", False))
        self.assertEqual(results.m_key("CoT SC (5-shots)"), results.m_key("CoT-SC"))
        self.assertEqual(results.m_key("AFlow (GPT-4o-mini)"), results.m_key("AFlow"))
        self.assertEqual(results.m_key("DMoA (Few-shot + TTT)"), results.m_key("DMoA (Ours)"))
        self.assertNotEqual(results.m_key("AFlow w/o search"), results.m_key("AFlow"))
        self.assertEqual(results._clean_name("$\\mathcal{ANN}$"), "ANN")
        self.assertEqual(results._clean_name("E$^3$-Orch"), "E³-Orch")
        self.assertEqual(results._clean_name("DyFlow（GPT-4.1）"), "DyFlow (GPT-4.1)")
        self.assertEqual(results.bb_key("gpt-4o-mini-2024-07-18"), results.bb_key("GPT-4o-mini"))
        self.assertEqual(results.bb_key("Qwen-3-8B"), results.bb_key("Qwen3-8B"))
        self.assertNotEqual(results.bb_key("Qwen2.5-7B"), results.bb_key("Qwen2.5-72B"))

    def _rows(self):
        from workbench.papers import results

        def row(pid, method, v, prop=False, n=None, pub="2025-01-01", bb="gpt-4o-mini"):
            sc = {"status": "subset" if n else "full", "n": n, "full": 1319, "split": None, "how": None, "where": None}
            return {"pid": pid, "paper": pid, "published": pub, "method": method, "method_raw": method, "name_status": "ok",
                    "mkey": results.m_key(method), "is_proposed": prop, "task_raw": "GSM8K", "dataset": "GSM8K", "category": "math",
                    "metric": "Accuracy", "metric_raw": "acc", "kind": "quality", "higher_better": True, "backbone": bb,
                    "bkey": results.bb_key(bb), "value": v, "unit": "%" if v > 1 else None, "where": "T1", "scale": sc,
                    "code": {"status": "open", "url": "https://github.com/x/y", "stars": 1}}
        return [row("p1", "AFlow", 93.5, True), row("p1", "CoT", 87.0), row("p3", "CoT", 90.1, bb="gpt-4o"),
                row("p2", "MaAS", 0.928, True, n=500, pub="2025-02-01"), row("p2", "AFlow", 91.0, n=500),
                row("p2", "AFlow w/o search", 80.0)]

    def test_dataset_table(self):
        from workbench.papers import results
        t = results.dataset_table(self._rows(), "GSM8K")
        cells = {mt["name"]: mt["cells"][0] for mt in t["rows"]}
        self.assertEqual(cells["AFlow"]["value"], 93.5)           # 优先方法自己的论文
        self.assertEqual(len(cells["AFlow"]["others"]), 1)
        self.assertEqual(cells["CoT"]["value"], 87.0)             # 不分 backbone；转引取中位数那条
        self.assertEqual(cells["MaAS"]["value"], 92.8)            # 0.928 换算成百分数
        self.assertEqual([mt["name"] for mt in t["rows"]], ["AFlow", "MaAS", "CoT"])  # 按主指标排序
        self.assertEqual(len(cells["CoT"]["others"]), 1)
        self.assertEqual(cells["AFlow"]["rank"], 1)
        self.assertNotIn("AFlow w/o search", cells)               # 消融默认隐藏
        self.assertTrue(t["mixed"])                                # 全量与子集混在一起
        self.assertEqual(t["rows"][0]["own"]["code"]["status"], "open")
        self.assertIsNone(next(mt for mt in t["rows"] if mt["name"] == "CoT")["own"])
        self.assertEqual(len(results.dataset_table(self._rows(), "GSM8K", full_only=True)["rows"]), 2)
        self.assertEqual(results.board(self._rows(), "math")["tables"][0]["dataset"], "GSM8K")
        self.assertEqual(results.board(self._rows(), "math", min_methods=9)["small"], [("GSM8K", 4)])

    def test_codecheck(self):
        from unittest import mock
        from workbench.papers import codecheck as cc
        resp = {"repos/a/b": (200, {"size": 10, "stargazers_count": 3}), "repos/a/b/contents": (200, [{"name": "README.md", "type": "file"}]),
                "repos/a/c": (200, {"size": 10}), "repos/a/c/contents": (200, [{"name": "README.md", "type": "file"}, {"name": "src", "type": "dir"}]),
                "repos/a/d": (404, None), "repos/a/e": (200, {"size": 0})}
        with mock.patch.object(cc, "_gh", side_effect=lambda ep: resp[ep]):
            self.assertEqual(cc.check_url("https://github.com/a/b")["status"], "readme_only")
            self.assertEqual(cc.check_url("https://github.com/a/c")["status"], "open")
            self.assertEqual(cc.check_url("https://github.com/a/d")["status"], "dead")
            self.assertEqual(cc.check_url("https://github.com/a/e")["status"], "empty")
            self.assertEqual(cc.check_url("https://anonymous.4open.science/r/X-1")["status"], "anonymous")
            with mock.patch.object(cc.http, "get", return_value='<a href="https://github.com/a/c">code</a>'):
                r = cc.check_url("https://proj.github.io/")
                self.assertEqual((r["status"], r["url_checked"]), ("open", "https://github.com/a/c"))


class TestResultsPipeline(unittest.TestCase):
    """假模型：速读带 eval_sets → 归类 → 老论文补评测规模 → 索引 → 论文式表。"""
    def setUp(self):
        from workbench.papers import store
        os.environ["WB_LLM_PROTOCOL"] = "fake"
        fieldmod.init_from("mas-arch-opt")
        for i in range(3):
            pid = f"arxiv-9999.1000{i}"
            store.save({"id": pid, "arxiv": pid.split("-")[1], "title": f"Agentic workflow {i}", "abstract": "multi-agent",
                        "intake": {"state": "relevant"}, "published": f"2026-0{i + 1}-01", "sources": []})
        store._cache.clear()

    def tearDown(self):
        os.environ.pop("WB_LLM_PROTOCOL", None)

    def test_flow(self):
        from workbench.papers import datasets, results, store
        pids = [f"arxiv-9999.1000{i}" for i in range(3)]
        self.assertEqual(intake.run_quickreads(pids, workers=2, log=lambda *a: None)["ok"], 3)
        self.assertIn("eval_sets", store.load_quickread(pids[0]))           # 新速读自带评测规模
        qr = store.load_quickread(pids[2])
        qr.pop("eval_sets")
        qr["results"].append({"method": "X", "task": "BrandNewBench", "metric": "acc", "value": 50, "where": "T2"})
        store.save_quickread(pids[2], qr)
        store._cache.clear()
        self.assertIn(pids[2], datasets.pending())
        datasets.fill(datasets.pending(), log=lambda *a: None)              # 老论文补
        self.assertEqual(datasets.pending(), [])
        datasets.classify(log=lambda *a: None)
        self.assertFalse(datasets.lookup("BrandNewBench").get("pending"))
        results.rebuild_index(log=lambda *a: None)
        rows = results.all_rows()
        self.assertTrue(rows and all(r["dataset"] for r in rows))
        t = results.dataset_table(rows, "GSM8K")
        self.assertEqual([c["metric"] for c in t["cols"]], ["Accuracy"])   # "Acc." 与 "accuracy" 合成一列
        self.assertIn("CoT", [mt["name"] for mt in t["rows"]])
        sts = {g["status"] for g in t["scales"]}
        self.assertTrue(sts <= {"full", "subset"} and sts)
        # 方法名核对：MASS 更正为 MANGO，原文里没有的方法不进表
        from workbench.papers import namecheck
        qr = store.load_quickread(pids[1])
        qr["results"] += [{"method": "MASS", "is_proposed": False, "task": "GSM8K", "metric": "acc", "value": 70, "where": "T3"},
                          {"method": "CountBug", "task": "GSM8K", "metric": "Accuracy", "value": 139.6, "where": "T3"},
                          {"method": "Bogus Avg.", "task": "GSM8K", "metric": "acc", "value": 1, "where": "T3"}]
        store.save_quickread(pids[1], qr)
        store._cache.clear()
        self.assertIn(pids[1], namecheck.pending())
        namecheck.fill(namecheck.pending(), log=lambda *a: None)
        results.rebuild_index(log=lambda *a: None)
        names = {r["method"]: r for r in results.all_rows() if r["pid"] == pids[1]}
        self.assertIn("MANGO", names)
        self.assertTrue(names["MANGO"]["is_proposed"])
        self.assertEqual(names["MANGO"]["name_status"], "renamed")
        self.assertNotIn("MASS", names)
        self.assertNotIn("Bogus Avg.", names)
        self.assertNotIn("CountBug", names)                          # 百分比指标超出 0–100 不进表


if __name__ == "__main__":
    unittest.main()
