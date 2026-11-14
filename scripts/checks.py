import re
import statistics

import segments

SOURCE_SCRIPT = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]+|[A-Za-z]{3,}")
HALF_PUNCT = re.compile(r"[\u4e00-\u9fff][,;:?!]|[,;:?!][\u4e00-\u9fff]")
ARTIFACTS = re.compile(r"```|\*\*|^译文[:：]|（译者?注")


def run(source, trans, glossary=(), scripture=()):
    issues = []

    def add(level, seg, code, msg):
        issues.append({"level": level, "seg": seg, "code": code, "msg": msg})

    missing, extra, empty = segments.compare_ids(source, trans)
    for seg in missing:
        add("error", seg, "missing", "漏段：译文中没有这一段")
    for seg in extra:
        add("error", seg, "extra", "多段：原文中没有这个编号")
    for seg in empty:
        add("error", seg, "empty", "空段")

    common = [k for k in source if k in trans and trans[k].strip() and source[k].strip()]
    ratios = {k: len(trans[k]) / len(source[k]) for k in common}
    if len(ratios) >= 4:
        median = statistics.median(ratios.values())
        for seg, r in ratios.items():
            if len(source[seg]) < 30:
                continue
            if r < 0.5 * median:
                add("warn", seg, "too_short", f"译文明显偏短（长度比 {r:.2f}，本章中位数 {median:.2f}），可能漏译")
            elif r > 1.8 * median:
                add("warn", seg, "too_long", f"译文明显偏长（长度比 {r:.2f}，本章中位数 {median:.2f}），可能增译")

    for seg in common:
        text = trans[seg]
        if m := SOURCE_SCRIPT.search(text):
            add("warn", seg, "untranslated", f"译文中残留外文：{m[0]}")
        if m := HALF_PUNCT.search(text):
            add("warn", seg, "punct", f"中文里出现半角标点：{m[0]}")
        if m := ARTIFACTS.search(text):
            add("warn", seg, "artifact", f"疑似模型输出残留：{m[0]}")

    term_total = term_ok = 0
    for row in glossary:
        avoid = [a for a in row.get("avoid", "").split("|") if a]
        listed = [s.strip() for s in re.split(r"[;；,，\s]+", row.get("segs", "")) if s.strip()]
        for seg in common:
            for a in avoid:
                if a in trans[seg] and row["target"] not in a and a not in row["target"]:
                    add("warn", seg, "term_avoid", f"出现了术语表要求避免的译法「{a}」（{row['source']} 应译「{row['target']}」）")
        for seg in listed:
            if seg in trans and seg in source:
                term_total += 1
                if row["target"] in trans[seg]:
                    term_ok += 1
                else:
                    add("warn", seg, "term_missing", f"{row['source']} 应译「{row['target']}」，本段未见")

    by_ref = {}
    for row in scripture:
        if row.get("kind") == "引用":
            by_ref.setdefault(row["ref"], []).append(row["seg"])
    for ref, segs in by_ref.items():
        if len(segs) > 1:
            add("info", segs[0], "repeat_quote", f"{ref} 在多处被引用（{', '.join(segs)}），请确认各处措辞一致")

    stats = {
        "segments": len(source),
        "translated": len(common),
        "completeness": round(len(common) / len(source), 4) if source else 0,
        "term_checked": term_total,
        "term_compliance": round(term_ok / term_total, 4) if term_total else None,
        "errors": sum(i["level"] == "error" for i in issues),
        "warnings": sum(i["level"] == "warn" for i in issues),
    }
    return issues, stats
