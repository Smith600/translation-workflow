import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agents
import checks
import common
import segments
import tw


class SegmentsTest(unittest.TestCase):
    def test_parse_and_dump_roundtrip(self):
        text = "前言，应忽略\n```\n[1.1] 第一段\n续行\n[1.2] second\nline\n```\n"
        segs = segments.parse(text)
        self.assertEqual(segs, {"1.1": "第一段续行", "1.2": "second line"})
        self.assertEqual(segments.parse(segments.dump(segs)), segs)

    def test_duplicate_id_rejected(self):
        with self.assertRaises(ValueError):
            segments.parse("[1.1] a\n[1.1] b\n")

    def test_ordering_is_numeric(self):
        self.assertEqual(segments.dump({"1.10": "b", "1.2": "a"}), "[1.2] a\n[1.10] b\n")

    def test_require_same_ids(self):
        src = {"1.1": "a", "1.2": "b"}
        with self.assertRaisesRegex(ValueError, "缺少段落 1.2"):
            segments.require_same_ids(src, {"1.1": "甲"})
        with self.assertRaisesRegex(ValueError, "多出段落 1.3"):
            segments.require_same_ids(src, {"1.1": "甲", "1.2": "乙", "1.3": "丙"})

    def test_split_notes_and_changed(self):
        body, notes = segments.split_notes("[1.1] 甲\n=== NOTES ===\n[1.1] 说明")
        self.assertEqual(segments.parse(body), {"1.1": "甲"})
        self.assertEqual(notes, "[1.1] 说明")
        self.assertEqual(segments.changed({"1.1": "甲", "1.2": "乙"}, {"1.1": "甲", "1.2": "丙"}), ["1.2"])


class ChecksTest(unittest.TestCase):
    def test_detects_missing_terms_and_artifacts(self):
        source = {f"1.{i}": "λόγος " * 10 for i in range(1, 7)}
        trans = {f"1.{i}": "这是一段长度正常的中文译文，没有问题。" for i in range(1, 6)}
        trans["1.2"] = "短"
        trans["1.3"] = "这里的司铎残留了 πρεσβύτερος，还有半角,标点。"
        glossary = [{"source": "πρεσβύτερος", "target": "长老", "avoid": "司铎", "segs": "1.3;1.4"}]
        issues, stats = checks.run(source, trans, glossary)
        codes = {(i["seg"], i["code"]) for i in issues}
        self.assertIn(("1.6", "missing"), codes)
        self.assertIn(("1.2", "too_short"), codes)
        self.assertIn(("1.3", "untranslated"), codes)
        self.assertIn(("1.3", "punct"), codes)
        self.assertIn(("1.3", "term_avoid"), codes)
        self.assertIn(("1.4", "term_missing"), codes)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["term_compliance"], 0.0)

    def test_clean_translation_passes(self):
        source = {"1.1": "Χάρις ὑμῖν καὶ εἰρήνη"}
        issues, stats = checks.run(source, {"1.1": "愿恩惠、平安归与你们。"})
        self.assertEqual(issues, [])
        self.assertEqual(stats["completeness"], 1.0)


class JsonTest(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(common.extract_json('说明\n```json\n[{"seg": "1.1"}]\n```\n完'), [{"seg": "1.1"}])
        self.assertEqual(common.extract_json('结果如下：[{"a": [1, 2]}] 以上'), [{"a": [1, 2]}])
        self.assertEqual(common.extract_json('{"x": [1]}'), {"x": [1]})
        with self.assertRaises(ValueError):
            common.extract_json("没有 JSON")

    def test_extract_json_repairs_quotes_and_prefers_last_block(self):
        text = ('```json\n[\n  {\n    "seg": "5.6",\n    "explanation": "意为"被驱逐"。"\n  }\n]\n```\n'
                '再想想，撤回。\n```json\n[]\n```\n')
        self.assertEqual(common.extract_json(text), [])
        self.assertEqual(common.extract_json(text.split("再想想")[0])[0]["explanation"], '意为"被驱逐"。')

    def test_json_list_validator(self):
        v = common.json_list(["seg", "reason"])
        self.assertEqual(v('[{"seg": "[1.2]", "reason": "r"}]')[0]["seg"], "1.2")
        with self.assertRaises(ValueError):
            v('[{"seg": "1.2"}]')


class IsolationTest(unittest.TestCase):
    def test_devin_command_uses_isolation_config(self):
        cmd = agents._command({"agent": "devin", "model": "m"}, Path("/t/p.md"), "x", Path("/t/c.json"))
        self.assertEqual(cmd[cmd.index("--config") + 1], "/t/c.json")
        self.assertEqual(cmd[-1], "-p")

    def test_template_always_blocks(self):
        text = agents.isolation_template()
        self.assertIn("exit 2", text)
        hook = json.loads(text)["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "")
        self.assertTrue(hook["hooks"][0]["command"].rstrip("'").endswith("exit 2"))

    def test_missing_hook_refuses_and_template_change_changes_cache_key(self):
        role = {"agent": "devin", "model": "m"}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(agents, "devin_version", return_value="1.0"):
            tpl = Path(d) / "iso.json"
            with mock.patch.object(agents, "ISOLATION", tpl):
                tpl.write_text('{"hooks": {}}', "utf-8")
                with self.assertRaises(RuntimeError):
                    agents.cache_path(role, "p")
                tpl.write_text('__HOOK_LOG__ exit 2', "utf-8")
                first = agents.cache_path(role, "p")
                tpl.write_text('__HOOK_LOG__ exit 2 ', "utf-8")
                self.assertNotEqual(first, agents.cache_path(role, "p"))

    def test_parse_hook_log_and_strip_banner(self):
        log = '{"tool_name":"read"}{"tool_name":"exec"}\n{"tool_name":"webfetch"}\n'
        self.assertEqual([c["tool_name"] for c in agents.parse_hook_log(log)], ["read", "exec", "webfetch"])
        self.assertEqual(agents.strip_banner("\n ✓ Logged in as x\n ✓ ok\n[5.1] 译文\n"), "[5.1] 译文\n")


class FakeBook:
    lang, title = "希腊文", "测试书"

    def __init__(self, root):
        self.root = Path(root)

    def source(self, ch):
        return {f"{ch}.1": "λόγος", f"{ch}.2": "θεός"}

    def work(self, ch):
        d = self.root / f"ch{ch:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def glossary(self, locked_only=True):
        return []

    def glossary_text(self):
        return "（暂无）"

    def style_guide(self):
        return "风格"

    def scripture_text(self, ch):
        return "（未标注）"


def fake_call(cfg, role, prompt, validate, info):
    ch = prompt.split("第 ")[1].split(" 章")[0]
    info.update(label=f"devin / {role}", cache_hit=False, blocked_tool_calls=[])
    return validate(f"[{ch}.1] 道\n[{ch}.2] 上帝\n=== NOTES ===\n[{ch}.1] 把握：高｜建议译注：否｜无")


class TranslateTest(unittest.TestCase):
    def test_only_skip_and_meta(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tw.agents, "call", side_effect=fake_call) as call:
            book = FakeBook(d)
            tw.translate({}, book, 3, only="a")
            work = book.work(3)
            self.assertTrue((work / "draft_a.txt").exists())
            self.assertFalse((work / "draft_b.txt").exists())
            meta = json.loads((work / "draft_a.meta.json").read_text("utf-8"))
            self.assertEqual(meta["translator"], "translator_a")
            for k in ("prompt_template", "prompt_rendered", "source", "style_guide", "glossary", "scripture"):
                self.assertEqual(len(meta["sha256"][k]), 64)
            tw.translate({}, book, 3, only="a")
            self.assertEqual(call.call_count, 1)
            tw.translate({}, book, 3, only="a", force=True)
            self.assertEqual(call.call_count, 2)

    def test_chapter_failure_does_not_stop_batch(self):
        done = []

        def flaky(cfg, book, ch, only, force):
            if ch == 2:
                raise RuntimeError("坏")
            done.append(ch)

        book = mock.Mock()
        book.parse_chapters.return_value = [1, 2, 3]
        args = mock.Mock(cmd="translate", book="x", ch="1-3", only="a", force=False)
        with mock.patch.object(tw, "Book", return_value=book), mock.patch.object(tw, "translate", side_effect=flaky):
            with self.assertRaises(SystemExit) as e:
                tw.cmd_stage(args, {})
        self.assertEqual(done, [1, 3])
        self.assertIn("2", str(e.exception.code))


if __name__ == "__main__":
    unittest.main()
