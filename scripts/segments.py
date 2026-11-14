import re
from pathlib import Path

TAG = re.compile(r"^\[(\d+)\.(\d+)\]\s*(.*)$")
NOTES_MARK = "=== NOTES ==="
CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def key(seg_id):
    ch, v = seg_id.split(".")
    return int(ch), int(v)


def split_notes(text):
    body, _, notes = text.partition(NOTES_MARK)
    return body, notes.strip()


def parse(text):
    segs, cur = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        m = TAG.match(line)
        if m:
            cur = f"{int(m[1])}.{int(m[2])}"
            if cur in segs:
                raise ValueError(f"重复的段落编号 [{cur}]")
            segs[cur] = m[3].strip()
        elif cur:
            prev = segs[cur]
            sep = "" if not prev or CJK.match(prev[-1]) or CJK.match(line[0]) else " "
            segs[cur] = prev + sep + line
    return segs


def dump(segs):
    return "".join(f"[{k}] {segs[k]}\n" for k in sorted(segs, key=key))


def load(path):
    return parse(Path(path).read_text("utf-8"))


def save(path, segs):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dump(segs), "utf-8")


def compare_ids(expected, actual):
    missing = sorted(set(expected) - set(actual), key=key)
    extra = sorted(set(actual) - set(expected), key=key)
    empty = sorted((k for k in actual if k in expected and not actual[k].strip()), key=key)
    return missing, extra, empty


def require_same_ids(expected, actual):
    missing, extra, empty = compare_ids(expected, actual)
    problems = []
    if missing:
        problems.append("缺少段落 " + ", ".join(missing))
    if extra:
        problems.append("多出段落 " + ", ".join(extra))
    if empty:
        problems.append("空段落 " + ", ".join(empty))
    if problems:
        raise ValueError("；".join(problems))
    return actual


def changed(a, b):
    return sorted((k for k in a if k in b and a[k].strip() != b[k].strip()), key=key)
