#!/usr/bin/env python3
"""从 Lake 1912 Loeb 版 OCR 文本中抽取《克莱门一书》英译，按 [章.节] 对齐。

OCR 噪音较多，结果仅作审校旁证。按节号对不齐的章，整章放在该章第 1 节之前（标注"整章"）。
用法：python3 scripts/import_lake_en.py <Lake_Vol1_GR-EN.txt> <books/1clement>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import segments

GREEK = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")
VERSE = re.compile(r"(?:^|(?<=[\s.!?”’\"')]))(\d{1,2})[.,] (?=[\"“‘(]?[A-Za-z])")
SMALLCAPS = re.compile(r"^(\d\. )?([A-Z][a-z]{1,3}[a-z]*)")


def clean(page):
    blocks, cur = [], []
    for line in page + [""]:
        if line.strip():
            cur.append(line.strip())
        elif cur:
            blocks.append(cur)
            cur = []
    keep = []
    for b in blocks:
        text = " ".join(b)
        greek = len(GREEK.findall(text))
        if greek > 0.2 * len(text) or len(text) < 4 or "CLEMENT" in text or "APOSTOLIC FATHERS" in text:
            continue
        if re.match(r"^[\d'‘’*†\"“]{1,2}\s", b[0]) and not VERSE.match(b[0]):
            continue
        if all(len(l) < 16 for l in b) and not VERSE.search(text):
            continue
        if re.fullmatch(r"[\[\]\dIVXLCΠῚ|lt.\s]+", text):
            continue
        keep.append(text)
    return " ".join(keep)


def split(text):
    text = re.sub(r"(\w)- (\w)", r"\1\2", text)
    text = re.sub(r"\s+", " ", text)
    salutation, _, rest = text.partition(" 1. ")
    parts = VERSE.split(" 1. " + rest)
    return salutation.strip(), [(int(parts[i]), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def align(observed, expected):
    """全局对齐 OCR 节号序列与原文节号序列（最长公共子序列）。未匹配的 OCR 片段并入前一节。"""
    n, m = len(observed), len(expected)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            hit = dp[i - 1][j - 1] + 1 if observed[i - 1][0] == expected[j - 1][1] else -1
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1], hit)
    i, j, pairs = n, m, {}
    while i and j:
        if observed[i - 1][0] == expected[j - 1][1] and dp[i][j] == dp[i - 1][j - 1] + 1:
            pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif dp[i][j] == dp[i - 1][j]:
            i -= 1
        else:
            j -= 1
    out, last = {}, None
    for i, (num, body) in enumerate(observed):
        if i in pairs:
            last = expected[pairs[i]]
            out[last] = body
        elif last:
            out[last] += f" {num}. {body}"
    return out


def main(ocr, book_dir):
    lines = Path(ocr).read_text("utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("THE FIRST EPISTLE OF CLEMENT") and i > 600)
    end = next(i for i, l in enumerate(lines) if "SECOND EPISTLE OF CLEMENT" in l and i > start)
    text = clean([l.rstrip() for l in lines[start + 2:end]])
    text = text.split("The Epistle of the Romans to the Corinthians")[0]
    salutation, observed = split(text)
    out = Path(book_dir) / "reference" / "en-lake1912"
    out.mkdir(parents=True, exist_ok=True)
    salutation = re.sub(r"^TO .?THE CORINTHIANS\s*", "", salutation)
    segments.save(out / "ch00.txt", {"0.1": salutation})
    src = Path(book_dir) / "source"
    expected = [segments.key(k) for ch in range(1, 66) for k in sorted(segments.load(src / f"ch{ch:02d}.txt"), key=segments.key)]
    aligned = align(observed, expected)
    missing = []
    for ch in range(1, 66):
        segs = {}
        for c, v in expected:
            if c == ch:
                body = aligned.get((c, v))
                if body is None:
                    missing.append(f"{c}.{v}")
                body = body or "（OCR 未识别出这一节的节号，其内容可能并在上一节中）"
                segs[f"{c}.{v}"] = SMALLCAPS.sub(lambda m: (m[1] or "") + m[2][0] + m[2][1:].lower(), body)
        segments.save(out / f"ch{ch:02d}.txt", segs)
    segments.save(out / "ch66.txt", {"66.1": "The Epistle of the Romans to the Corinthians."})
    (out / "SOURCE.md").write_text(
        "Kirsopp Lake, *The Apostolic Fathers*, vol. 1 (Loeb Classical Library, 1912), 英译部分。公有领域。\n"
        "由 archive.org OCR 文本（apostolic_fathers/loeb_lake_v1/Lake_Vol1_GR-EN.txt）用 scripts/import_lake_en.py 自动抽取，"
        "含 OCR 错误，节的边界也可能有少量错位，仅作审校旁证，不要引用。\n", "utf-8")
    print(f"OCR 识别出 {len(observed)} 个节号片段，对齐到原文 {len(expected)} 节，"
          f"{len(missing)} 节未对齐：{', '.join(missing) or '无'}")


if __name__ == "__main__":
    main(*sys.argv[1:])
