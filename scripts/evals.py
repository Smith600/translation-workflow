import hashlib
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import agents
import checks
import segments
from common import json_list, read_csv, read_json, render, seg_sort, write_csv, write_json

CATEGORIES = {"mistranslation": "误译", "omission": "漏译", "addition": "增译", "terminology": "术语",
              "scripture": "经文引用", "fluency": "不通顺", "register": "语体"}
SEVERITIES = {"critical": "致命", "major": "重", "minor": "轻"}
SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}


def _chars(segs):
    return sum(len(t) for t in segs.values()) or 1


def _penalty(cfg, items):
    weights = cfg["eval"]["weights"]
    return sum(weights[i["severity"]] for i in items)


def _severity_validator(required, categories=None):
    base = json_list(required)

    def validate(text):
        items = base(text)
        for i in items:
            i["severity"] = str(i["severity"]).strip().lower()
            if i["severity"] not in SEVERITIES:
                raise ValueError(f"未知的严重度 {i['severity']}")
            if categories and i.get("category") not in categories:
                raise ValueError(f"未知的错误类别 {i.get('category')}")
        return items
    return validate


def _choice_validator(required, fields, choices):
    base = json_list(required)

    def validate(text):
        items = base(text)
        for i in items:
            for f in fields:
                i[f] = str(i[f]).strip().lower()
                if i[f] not in choices:
                    raise ValueError(f"字段 {f} 的取值 {i[f]} 不合法")
        return items
    return validate


def mqm(cfg, book, ch, version, judges=None):
    source, trans = book.source(ch), book.version(version, ch)
    prompt = render("eval_mqm", source_lang=book.lang, book_title=book.title, chapter=ch,
                    glossary=book.glossary_text(), style_guide=book.style_guide(),
                    source=segments.dump(source), translation=segments.dump(trans))
    judges = judges or cfg["eval"]["judges"]
    validator = _severity_validator(["seg", "category", "severity", "explanation"], CATEGORIES)
    with ThreadPoolExecutor(len(judges)) as ex:
        raw = dict(zip(judges, ex.map(lambda j: agents.call(cfg, j, prompt, validator), judges)))
    results = {}
    for judge, items in raw.items():
        items = seg_sort(i for i in items if i["seg"] in source)
        penalty, chars = _penalty(cfg, items), _chars(trans)
        result = {
            "method": "mqm", "version": version, "judge": judge, "model": agents.label(cfg["roles"][judge]),
            "summary": {
                "penalty": penalty, "chars": chars, "per_1000": round(penalty / chars * 1000, 1),
                "by_severity": dict(Counter(i["severity"] for i in items)),
                "by_category": dict(Counter(i["category"] for i in items)),
            },
            "items": items,
        }
        write_json(book.evald(ch) / f"mqm__{version}__{judge}.json", result)
        results[judge] = result
        s = result["summary"]
        print(f"  MQM [{version}] {judge}: 每千字扣 {s['per_1000']} 分 {s['by_severity']}")
    return results


def _flip(seg, a, b):
    return int(hashlib.sha256(f"{seg}|{a}|{b}".encode()).hexdigest(), 16) % 2 == 1


def pairwise(cfg, book, ch, a, b):
    source, ta, tb = book.source(ch), book.version(a, ch), book.version(b, ch)
    segs = [k for k in source if k in ta and k in tb and ta[k].strip() != tb[k].strip()]
    identical = len([k for k in source if k in ta and k in tb]) - len(segs)
    if not segs:
        print("  两个版本完全相同，无需比较")
        return {}
    order = {k: (b, a) if _flip(k, a, b) else (a, b) for k in segs}
    texts = {a: ta, b: tb}
    pairs = "\n\n".join(f"[{k}]\n译文1：{texts[order[k][0]][k]}\n译文2：{texts[order[k][1]][k]}" for k in segs)
    prompt = render("eval_pairwise", source_lang=book.lang, book_title=book.title, chapter=ch,
                    source=segments.dump({k: source[k] for k in segs}), pairs=pairs)
    results = {}
    for judge in cfg["eval"]["judges"]:
        raw = agents.call(cfg, judge, prompt, _choice_validator(
            ["seg", "fidelity", "readability"], ["fidelity", "readability"], {"1", "2", "tie"}))
        items, tally = [], {"fidelity": Counter(), "readability": Counter()}
        for i in raw:
            if i["seg"] not in order:
                continue
            item = {"seg": i["seg"], "reason": i.get("reason", "")}
            for f in tally:
                item[f] = "tie" if i[f] == "tie" else order[i["seg"]][int(i[f]) - 1]
                tally[f][item[f]] += 1
            items.append(item)
        result = {"method": "pairwise", "a": a, "b": b, "judge": judge, "model": agents.label(cfg["roles"][judge]),
                  "summary": {"compared": len(items), "identical": identical,
                              **{f: {a: t[a], b: t[b], "tie": t["tie"]} for f, t in tally.items()}},
                  "items": seg_sort(items)}
        write_json(book.evald(ch) / f"pairwise__{a}__vs__{b}__{judge}.json", result)
        results[judge] = result
        s = result["summary"]
        print(f"  盲评 {judge}: 忠实 {s['fidelity']}  可读 {s['readability']}")
    return results


def backtranslate(cfg, book, ch, version):
    source, trans = book.source(ch), book.version(version, ch)
    back = agents.call(cfg, "backtranslator", render("eval_backtranslate", translation=segments.dump(trans)),
                       lambda t: segments.require_same_ids(trans, segments.parse(t)))
    segments.save(book.evald(ch) / f"backtext__{version}.txt", back)
    judge = cfg["eval"]["judges"][0]
    items = agents.call(cfg, judge, render(
        "eval_backcompare", source_lang=book.lang, book_title=book.title, chapter=ch,
        source=segments.dump(source), backtranslation=segments.dump(back)),
        _severity_validator(["seg", "severity", "explanation"]))
    items = seg_sort(i for i in items if i["seg"] in source)
    penalty, chars = _penalty(cfg, items), _chars(trans)
    result = {"method": "backtranslation", "version": version, "judge": judge,
              "summary": {"deviations": len(items), "per_1000": round(penalty / chars * 1000, 1),
                          "by_severity": dict(Counter(i["severity"] for i in items))},
              "items": items}
    write_json(book.evald(ch) / f"backtranslation__{version}.json", result)
    print(f"  回译 [{version}]: {len(items)} 处偏离 {result['summary']['by_severity']}")
    return result


def qa(cfg, book, ch, version):
    source, trans = book.source(ch), book.version(version, ch)
    questions = agents.call(cfg, "qa_writer", render(
        "eval_qa_generate", source_lang=book.lang, book_title=book.title, chapter=ch,
        count=min(cfg["eval"]["qa_questions"], max(3, len(source))), source=segments.dump(source)),
        json_list(["seg", "question", "answer"]))
    n = len(questions)

    def indexed(required):
        base = json_list(required)

        def validate(text):
            by_i = {int(x["i"]): x for x in base(text)}
            if set(by_i) != set(range(1, n + 1)):
                raise ValueError(f"题号应为 1..{n}")
            return by_i
        return validate

    answers = agents.call(cfg, "qa_answerer", render(
        "eval_qa_answer", translation="\n".join(trans[k] for k in sorted(trans, key=segments.key)),
        questions="\n".join(f"{i}. {q['question']}" for i, q in enumerate(questions, 1))),
        indexed(["i", "answer"]))
    grades = agents.call(cfg, "qa_writer", render("eval_qa_grade", items="\n\n".join(
        f"题 {i}：{q['question']}\n标准答案：{q['answer']}\n答题者的回答：{answers[i]['answer']}"
        for i, q in enumerate(questions, 1))), indexed(["i", "grade"]))
    items = [{"seg": q["seg"], "question": q["question"], "expected": q["answer"], "answer": answers[i]["answer"],
              "grade": str(grades[i]["grade"]).strip().lower(), "reason": grades[i].get("reason", "")}
             for i, q in enumerate(questions, 1)]
    tally = Counter(i["grade"] for i in items)
    score = round((tally["correct"] + 0.5 * tally["partial"]) / n, 3)
    result = {"method": "qa", "version": version,
              "summary": {"questions": n, "score": score, "correct": tally["correct"],
                          "partial": tally["partial"], "wrong": n - tally["correct"] - tally["partial"]},
              "items": items}
    write_json(book.evald(ch) / f"qa__{version}.json", result)
    print(f"  问答 [{version}]: 得分 {score:.0%}（{tally['correct']} 对 / {tally['partial']} 半对 / {n} 题）")
    return result


def seed(cfg, book, ch, version):
    source, trans = book.source(ch), book.version(version, ch)
    count = min(cfg["eval"]["seed_errors"], max(1, len(source) // 2))
    seeded_items = agents.call(cfg, "seeder", render(
        "eval_seed_errors", source_lang=book.lang, book_title=book.title, chapter=ch, count=count,
        source=segments.dump(source), translation=segments.dump(trans)),
        json_list(["seg", "type", "new_text", "description"]))
    seeded_items = [i for i in seeded_items if i["seg"] in trans and i["new_text"].strip() != trans[i["seg"]].strip()]
    seeded = dict(trans)
    for i in seeded_items:
        seeded[i["seg"]] = segments.TAG.sub(r"\3", i["new_text"].strip())
    seeded_version = f"seeded_{version}"
    segments.save(book.evald(ch) / f"{seeded_version}.txt", seeded)
    clean, dirty = mqm(cfg, book, ch, version), mqm(cfg, book, ch, seeded_version)

    def profile(result, seg):
        found = [i for i in result["items"] if i["seg"] == seg]
        return len(found), max((SEVERITY_RANK[i["severity"]] for i in found), default=0)

    judges = {}
    for judge in dirty:
        hits = [i["seg"] for i in seeded_items if profile(dirty[judge], i["seg"]) > profile(clean[judge], i["seg"])
                or profile(dirty[judge], i["seg"])[1] > profile(clean[judge], i["seg"])[1]]
        judges[judge] = {"model": agents.label(cfg["roles"][judge]), "detected": hits,
                         "recall": round(len(hits) / len(seeded_items), 3) if seeded_items else None}
        print(f"  埋错校准 {judge}: 检出 {len(hits)}/{len(seeded_items)}")
    result = {"method": "calibration", "version": version,
              "summary": {"seeded": len(seeded_items), "recall": {j: v["recall"] for j, v in judges.items()}},
              "judges": judges, "items": seg_sort(seeded_items)}
    write_json(book.evald(ch) / f"calibration__{version}.json", result)
    return result


def human(book, ch, a, b=None):
    name = f"human__{a}" + (f"__vs__{b}" if b else "")
    sheet, out = book.evald(ch) / f"{name}.csv", book.evald(ch) / f"{name}.json"
    ta = book.version(a, ch)
    if not sheet.exists():
        if b:
            tb = book.version(b, ch)
            segs = [k for k in ta if k in tb and ta[k].strip() != tb[k].strip()]
            rows = [{"seg": k, "译文1": (tb if _flip(k, a, b) else ta)[k], "译文2": (ta if _flip(k, a, b) else tb)[k],
                     "更通顺(填1/2/0)": "", "备注": ""} for k in sorted(segs, key=segments.key)]
        else:
            rows = [{"seg": k, "译文": ta[k], "可读性(1-5)": "", "读不懂(填x)": "", "备注": ""}
                    for k in sorted(ta, key=segments.key)]
        write_csv(sheet, list(rows[0]), rows)
        print(f"  已生成人工评分表：{sheet}\n  用 Excel/Numbers 打开填写后保存，再运行同一条命令即可计分。")
        return None
    rows = read_csv(sheet)
    if b:
        tally = Counter()
        for r in rows:
            v = r.get("更通顺(填1/2/0)", "")
            if v in ("1", "2"):
                tally[(a, b)[(v == "2") != _flip(r["seg"], a, b)]] += 1
            elif v == "0":
                tally["tie"] += 1
        summary = {"rated": sum(tally.values()), a: tally[a], b: tally[b], "tie": tally["tie"]}
        result = {"method": "human_pairwise", "a": a, "b": b, "summary": summary}
    else:
        scores = [int(r["可读性(1-5)"]) for r in rows if r.get("可读性(1-5)", "").isdigit()]
        unclear = [r["seg"] for r in rows if r.get("读不懂(填x)")]
        summary = {"rated": len(scores), "mean": round(sum(scores) / len(scores), 2) if scores else None,
                   "unclear": unclear}
        result = {"method": "human_readability", "version": a, "summary": summary,
                  "items": [{"seg": r["seg"], "note": r["备注"]} for r in rows if r.get("备注")]}
    if not summary["rated"]:
        sys.exit(f"评分表还没有填写：{sheet}")
    write_json(out, result)
    print(f"  人工评分: {summary}")
    return result


def mechanical(book, ch, version):
    issues, stats = checks.run(book.source(ch), book.version(version, ch), book.glossary(), book.scripture(ch))
    write_json(book.work(ch) / f"check__{version}.json", {"version": version, "stats": stats, "issues": issues})
    return issues, stats


def _load(book, ch, pattern):
    return [read_json(p) for p in sorted(book.evald(ch).glob(pattern))]


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def report(cfg, book, chs):
    lines = [f"# 《{book.title}》翻译评估成绩单", "",
             "分数怎么读：**MQM 每千字扣分**越低越好（轻错 1 分、重错 5 分、致命错 25 分）；"
             "**回译偏离**越少越好；**问答得分**越高越好；**检出率**是评审模型发现故意植入错误的比例，"
             "它告诉你评审本身有多可信。", ""]
    totals, pair_totals, calib = {}, {}, {}
    chapter_rows = []
    for ch in chs:
        if not (book.dir / "eval" / f"ch{ch:02d}").exists():
            continue
        versions = {}
        for r in _load(book, ch, "mqm__*.json"):
            if r["version"].startswith("seeded_"):
                continue
            v = versions.setdefault(r["version"], {"mqm": {}})
            v["mqm"][r["judge"]] = r["summary"]
            t = totals.setdefault(r["version"], {}).setdefault(r["judge"], Counter())
            t.update(penalty=r["summary"]["penalty"], chars=r["summary"]["chars"],
                     **{f"sev_{k}": n for k, n in r["summary"]["by_severity"].items()})
        for r in _load(book, ch, "backtranslation__*.json"):
            versions.setdefault(r["version"], {"mqm": {}})["back"] = r["summary"]
        for r in _load(book, ch, "qa__*.json"):
            versions.setdefault(r["version"], {"mqm": {}})["qa"] = r["summary"]
        for r in _load(book, ch, "pairwise__*.json"):
            t = pair_totals.setdefault((r["a"], r["b"]), {}).setdefault(r["judge"], Counter())
            for f in ("fidelity", "readability"):
                t.update({f"{f}_{k}": n for k, n in zip("abt", (r["summary"][f][r["a"]], r["summary"][f][r["b"]],
                                                                 r["summary"][f]["tie"]))})
        for r in _load(book, ch, "human__*.json"):
            if r["method"] == "human_readability":
                versions.setdefault(r["version"], {"mqm": {}})["human"] = r["summary"]
            else:
                t = pair_totals.setdefault((r["a"], r["b"]), {}).setdefault("你本人", Counter())
                t.update(readability_a=r["summary"][r["a"]], readability_b=r["summary"][r["b"]],
                         readability_t=r["summary"]["tie"])
        for r in _load(book, ch, "calibration__*.json"):
            for j, v in r["judges"].items():
                calib.setdefault(j, Counter()).update(seeded=r["summary"]["seeded"], hit=len(v["detected"]))
        for name, v in sorted(versions.items()):
            path = book.work(ch) / f"check__{name}.json"
            stats = read_json(path, {}).get("stats", {})
            mq = " / ".join(f"{s['per_1000']}" for _, s in sorted(v["mqm"].items())) or "—"
            sev = Counter()
            for s in v["mqm"].values():
                sev.update(s["by_severity"])
            back = v.get("back")
            chapter_rows.append(
                f"| {ch} | {name} | {mq} | {sev['critical']}/{sev['major']}/{sev['minor']} | "
                f"{back['deviations'] if back else '—'} | {_pct(v['qa']['score']) if 'qa' in v else '—'} | "
                f"{v['human']['mean'] if 'human' in v else '—'} | {_pct(stats.get('completeness'))} | "
                f"{_pct(stats.get('term_compliance'))} |")

    judges = sorted({j for v in totals.values() for j in v})
    lines += ["## 总览：各版本 MQM 每千字扣分（越低越好）", "",
              "| 版本 | " + " | ".join(f"{j}（{(agents.label(cfg['roles'][j]) if j in cfg['roles'] else '你本人')}）" for j in judges)
              + " | 平均 | 致命/重/轻（各评审合计） |", "|---|" + "---|" * (len(judges) + 2)]
    for version, by_judge in sorted(totals.items()):
        scores = {j: t["penalty"] / t["chars"] * 1000 for j, t in by_judge.items()}
        sev = sum(by_judge.values(), Counter())
        lines.append(f"| {version} | " + " | ".join(f"{scores[j]:.1f}" if j in scores else "—" for j in judges)
                     + f" | {sum(scores.values()) / len(scores):.1f} | "
                     f"{sev['sev_critical']}/{sev['sev_major']}/{sev['sev_minor']} |")
    if pair_totals:
        lines += ["", "## 盲评两两对比（胜 : 负 : 平）", "", "| 对比 | 评审 | 忠实 | 可读 |", "|---|---|---|---|"]
        for (a, b), by_judge in sorted(pair_totals.items()):
            for j, t in sorted(by_judge.items()):
                fid = f"{t['fidelity_a']} : {t['fidelity_b']} : {t['fidelity_t']}" if j != "你本人" else "—"
                lines.append(f"| {a} 对 {b} | {j} | {fid} | "
                             f"{t['readability_a']} : {t['readability_b']} : {t['readability_t']} |")
    if calib:
        lines += ["", "## 评审可信度（埋错校准）", "", "| 评审 | 模型 | 植入错误数 | 检出数 | 检出率 |", "|---|---|---|---|---|"]
        for j, t in sorted(calib.items()):
            lines.append(f"| {j} | {(agents.label(cfg['roles'][j]) if j in cfg['roles'] else '你本人')} | {t['seeded']} | {t['hit']} | "
                         f"{t['hit'] / t['seeded']:.0%} |")
    lines += ["", "## 分章明细", "",
              "| 章 | 版本 | MQM 每千字扣分（各评审） | 致命/重/轻 | 回译偏离 | 问答得分 | 人工可读性 | 完整性 | 术语遵守率 |",
              "|---|---|---|---|---|---|---|---|---|", *chapter_rows, ""]
    path = book.dir / "eval" / "report.md"
    path.write_text("\n".join(lines), "utf-8")
    print(f"成绩单已生成：{path}")
    return path
