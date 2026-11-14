#!/usr/bin/env python3
"""导入每行一节的原文（格式：`章.节 原文`，如 Open Apostolic Fathers），按章写成 source/chNN.txt。

非数字编号的行（如卷末题署 `SB.1`）用 --extra 指定放到哪一章，如 --extra SB=66。
用法：python3 scripts/import_segmented.py <输入文件> <books/书名> [--extra SB=66]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import segments


def main():
    p = argparse.ArgumentParser()
    p.add_argument("input")
    p.add_argument("book_dir")
    p.add_argument("--extra", action="append", default=[], help="非数字章号映射，如 SB=66")
    args = p.parse_args()
    extra = dict(e.split("=") for e in args.extra)
    chapters = {}
    for n, line in enumerate(Path(args.input).read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        m = re.match(r"^(\w+)\.(\d+)\s+(.+)$", line.strip())
        if not m:
            sys.exit(f"第 {n} 行格式无法识别：{line[:60]}")
        ch = int(m[1]) if m[1].isdigit() else int(extra.get(m[1], -1))
        if ch < 0:
            sys.exit(f"第 {n} 行的章号 {m[1]} 不是数字，请用 --extra {m[1]}=章号 指定")
        seg = f"{ch}.{int(m[2])}"
        if seg in chapters.setdefault(ch, {}):
            sys.exit(f"重复编号 {seg}")
        chapters[ch][seg] = m[3].strip()
    out = Path(args.book_dir) / "source"
    for ch, segs in chapters.items():
        segments.save(out / f"ch{ch:02d}.txt", segs)
    print(f"已导入 {len(chapters)} 章、{sum(map(len, chapters.values()))} 节到 {out}")


if __name__ == "__main__":
    main()
