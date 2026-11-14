import csv
import json
import re
import sys
import tomllib
from pathlib import Path

import segments

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_FIELDS = ["source", "target", "avoid", "category", "segs", "status", "note"]
SCRIPTURE_FIELDS = ["seg", "ref", "kind", "note"]
NOTE_FIELDS = ["seg", "note"]


def load_config():
    return tomllib.loads((ROOT / "config" / "pipeline.toml").read_text("utf-8"))


def render(template, **values):
    text = (ROOT / "prompts" / f"{template}.md").read_text("utf-8")
    needed = set(re.findall(r"\{\{(\w+)\}\}", text))
    if needed - set(values):
        raise KeyError(f"提示词 {template} 缺少变量: {needed - set(values)}")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values[m[1]]), text)


def read_csv(path):
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def write_csv(path, fields, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_json(path, default=None):
    return json.loads(Path(path).read_text("utf-8")) if Path(path).exists() else default


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")


JSON_LINE = re.compile(r'^(\s*"[^"]+"\s*:\s*")(.*)("\s*,?\s*)$')


def _repair_quotes(text):
    """模型常把英文双引号当中文引号写进字符串值里（如 "意为"被驱逐""），逐行把值内部的引号转义。"""
    def fix(line):
        m = JSON_LINE.match(line)
        return m[1] + re.sub(r'(?<!\\)"', r'\\"', m[2]) + m[3] if m else line
    return "\n".join(fix(l) for l in text.splitlines())


def extract_json(text):
    candidates = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)[::-1]
    spans = [(text.find(o), text.rfind(c)) for o, c in ("[]", "{}")]
    candidates += [text[i:j + 1] for i, j in sorted(s for s in spans if 0 <= s[0] < s[1])]
    for c in candidates:
        for attempt in (c, _repair_quotes(c)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError("输出中找不到合法的 JSON")


def json_list(required):
    def validate(text):
        data = extract_json(text)
        if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
            raise ValueError("JSON 应为对象列表")
        for item in data:
            if set(required) - set(item):
                raise ValueError(f"JSON 条目缺少字段 {set(required) - set(item)}")
            if "seg" in item:
                item["seg"] = str(item["seg"]).strip("[] ")
        return data
    return validate


def seg_sort(items):
    def k(item):
        try:
            return segments.key(item["seg"])
        except (ValueError, KeyError):
            return (10**6, 0)
    return sorted(items, key=k)


class Book:
    def __init__(self, name):
        self.name = name
        self.dir = ROOT / "books" / name
        if not (self.dir / "book.toml").exists():
            sys.exit(f"找不到 {self.dir / 'book.toml'}，请先运行: tw.py init {name}")
        self.meta = tomllib.loads((self.dir / "book.toml").read_text("utf-8"))
        self.profile = ROOT / "profiles" / self.meta["profile"]
        self.title = self.meta["title_zh"]
        self.lang = self.meta["source_lang"]

    def chapters(self):
        return sorted(int(p.stem[2:]) for p in (self.dir / "source").glob("ch*.txt"))

    def parse_chapters(self, spec):
        if spec in (None, "all"):
            return self.chapters()
        if spec == "bench":
            return list(self.meta.get("benchmark_chapters", []))
        out = []
        for part in spec.split(","):
            a, _, b = part.partition("-")
            out += range(int(a), int(b or a) + 1)
        return out

    def source(self, ch):
        path = self.dir / "source" / f"ch{ch:02d}.txt"
        if not path.exists():
            sys.exit(f"找不到原文 {path}")
        return segments.load(path)

    def references(self, ch):
        refs = {}
        for d in sorted((self.dir / "reference").glob("*")):
            path = d / f"ch{ch:02d}.txt"
            if path.exists():
                refs[d.name] = segments.load(path)
        return refs

    def references_text(self, ch):
        refs = self.references(ch)
        return "\n\n".join(f"### {name}\n{segments.dump(s)}" for name, s in refs.items()) or "（无）"

    def work(self, ch):
        d = self.dir / "work" / f"ch{ch:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def evald(self, ch):
        d = self.dir / "eval" / f"ch{ch:02d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def final_path(self, ch):
        return self.dir / "final" / f"ch{ch:02d}.txt"

    def version_path(self, version, ch):
        candidates = [
            self.final_path(ch) if version == "final" else None,
            self.dir / "work" / f"ch{ch:02d}" / f"{version}.txt",
            self.dir / "baseline" / version / f"ch{ch:02d}.txt",
            self.dir / "eval" / f"ch{ch:02d}" / f"{version}.txt",
        ]
        for p in candidates:
            if p and p.exists():
                return p
        sys.exit(f"找不到第 {ch} 章的版本「{version}」（查找过 final/、work/、baseline/{version}/、eval/）")

    def version(self, version, ch):
        return segments.load(self.version_path(version, ch))

    def glossary(self, locked_only=True):
        merged = {}
        for path in (self.profile / "glossary_base.csv", self.dir / "glossary.csv"):
            merged.update({r["source"]: r for r in read_csv(path)})
        return [r for r in merged.values() if r.get("source") and r.get("target")
                and (not locked_only or r.get("status") == "locked")]

    def glossary_text(self):
        lines = []
        for r in self.glossary():
            line = f"- {r['source']} → {r['target']}"
            if r.get("avoid"):
                line += f"（不要译作：{r['avoid'].replace('|', '、')}）"
            if r.get("note"):
                line += f"　〔{r['note']}〕"
            lines.append(line)
        return "\n".join(lines) or "（暂无）"

    def style_guide(self):
        return (self.profile / "style_guide.md").read_text("utf-8")

    def scripture(self, ch=None):
        rows = read_csv(self.dir / "scripture_index.csv")
        return [r for r in rows if ch is None or r["seg"].split(".")[0] == str(ch)]

    def notes(self, ch=None):
        rows = read_csv(self.dir / "notes.csv")
        return [r for r in rows if ch is None or r["seg"].split(".")[0] == str(ch)]

    def scripture_text(self, ch):
        rows = self.scripture(ch)
        return "\n".join(f"- [{r['seg']}] {r['ref']}（{r['kind']}）{r.get('note', '')}" for r in rows) or "（未标注）"
