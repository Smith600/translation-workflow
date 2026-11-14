import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import checks
import common
import segments


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


if __name__ == "__main__":
    unittest.main()
