#!/usr/bin/env python3
import argparse
import datetime
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agents
import evals
import segments
from common import (GLOSSARY_FIELDS, NOTE_FIELDS, ROOT, SCRIPTURE_FIELDS, Book, extract_json, json_list,
                    load_config, read_csv, read_json, render, seg_sort, write_csv, write_json)

BOOK_TOML = '''title_zh = ""
title_original = ""
author_zh = ""
source_lang = "希腊文"
profile = "protestant-cuv"
benchmark_chapters = []
license = "CC BY-SA 4.0"   # 若底本为纯公有领域（无 CC 许可的数字本），可改为 "CC0 1.0（公有领域贡献）"

[source]
edition = ""
url = ""
copyright = "公有领域"
'''


def cmd_init(args, cfg):
    d = ROOT / "books" / args.book
    if (d / "book.toml").exists():
        sys.exit(f"{d} 已存在")
    for sub in ("source", "reference", "work", "final", "eval", "baseline"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "book.toml").write_text(BOOK_TOML, "utf-8")
    write_csv(d / "glossary.csv", GLOSSARY_FIELDS, [])
    write_csv(d / "scripture_index.csv", SCRIPTURE_FIELDS, [])
    write_csv(d / "notes.csv", NOTE_FIELDS, [])
    (d / "decisions.md").write_text("# 人工裁决记录\n\n", "utf-8")
    print(f"已创建 {d}\n下一步：填写 book.toml，把分段原文放进 source/chNN.txt（每段一行：[章.节] 原文）")


def cmd_glossary(args, cfg):
    book = Book(args.book)
    chs = book.parse_chapters(args.ch)
    path = book.dir / "glossary.csv"
    rows = read_csv(path)
    known = {r["source"]: r for r in rows}
    for i in range(0, len(chs), args.batch):
        batch = chs[i:i + args.batch]
        print(f"抽取术语：第 {batch[0]}–{batch[-1]} 章")
        source = "\n".join(segments.dump(book.source(ch)) for ch in batch)
        existing = "\n".join(f"- {r['source']} → {r['target']}" for r in [*book.glossary(False), *known.values()])
        items = agents.call(cfg, "preparer", render(
            "glossary_extract", source_lang=book.lang, book_title=book.title, style_guide=book.style_guide(),
            glossary=existing or "（暂无）", source=source), json_list(["source", "target"]))
        for item in items:
            if item["source"] in known:
                old = known[item["source"]]
                merged = dict.fromkeys(filter(None, (old.get("segs", "") + ";" + str(item.get("segs", ""))).split(";")))
                old["segs"] = ";".join(merged)
            else:
                row = {f: str(item.get(f, "")) for f in GLOSSARY_FIELDS} | {"status": "proposed"}
                known[row["source"]] = row
                rows.append(row)
    write_csv(path, GLOSSARY_FIELDS, rows)
    pending = sum(r["status"] != "locked" for r in rows)
    print(f"术语表共 {len(rows)} 条，其中 {pending} 条待确认：{path}\n"
          "请逐条检查：同意就把 status 改为 locked；不同意先改 target 再改为 locked；不需要的行直接删除。")


def cmd_scripture(args, cfg):
    book = Book(args.book)
    chs = book.parse_chapters(args.ch)

    def one(ch):
        return ch, agents.call(cfg, "preparer", render(
            "scripture_index", source_lang=book.lang, book_title=book.title, chapter=ch,
            style_guide=book.style_guide(), source=segments.dump(book.source(ch))), json_list(["seg", "ref", "kind"]))

    with ThreadPoolExecutor(4) as ex:
        results = dict(ex.map(one, chs))
    rows = [r for r in book.scripture() if int(r["seg"].split(".")[0]) not in results]
    for ch, items in results.items():
        rows += [i for i in items if i["seg"] in book.source(ch)]
    write_csv(book.dir / "scripture_index.csv", SCRIPTURE_FIELDS, seg_sort(rows))
    print(f"经文引用索引已更新（{len(chs)} 章，共 {len(rows)} 条）：{book.dir / 'scripture_index.csv'}")


def _translation_validator(source, with_json=None):
    def validate(text):
        body, notes = segments.split_notes(text)
        segs = segments.require_same_ids(source, segments.parse(body))
        return segs, (json_list(with_json)(notes or "[]") if with_json else notes)
    return validate


def translate(cfg, book, ch, only="ab", force=False):
    source, work = book.source(ch), book.work(ch)
    todo = [x for x in only if force or not ((work / f"draft_{x}.txt").exists() and (work / f"draft_{x}.meta.json").exists())]
    if not todo:
        print(f"  已有初译（{', '.join(f'draft_{x}' for x in only)}），跳过；如需重译请加 --force")
        return
    pending = [r for r in book.glossary(False) if r.get("status") != "locked"]
    if pending:
        print(f"  提醒：术语表（基础表 + 本书表）中有 {len(pending)} 条尚未锁定（status≠locked），本次翻译不会使用它们")
    inputs = {"source": segments.dump(source), "style_guide": book.style_guide(), "glossary": book.glossary_text(),
              "scripture": book.scripture_text(ch)}
    prompt = render("translate", source_lang=book.lang, book_title=book.title, chapter=ch, **inputs)
    infos = {x: {} for x in todo}
    with ThreadPoolExecutor(len(todo)) as ex:
        futures = {x: ex.submit(agents.call, cfg, f"translator_{x}", prompt, _translation_validator(source), infos[x])
                   for x in todo}
    for x, fut in futures.items():
        segs, notes = fut.result()
        segments.save(work / f"draft_{x}.txt", segs)
        (work / f"notes_{x}.md").write_text(notes + "\n", "utf-8")
        write_json(work / f"draft_{x}.meta.json", {
            "translator": f"translator_{x}", "chapter": ch, **infos[x],
            "sha256": {"prompt_template": agents.sha256((ROOT / "prompts" / "translate.md").read_text("utf-8")),
                       "prompt_rendered": agents.sha256(prompt), **{k: agents.sha256(v) for k, v in inputs.items()}},
            "written_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
    print(f"  初译完成：{', '.join(f'{work}/draft_{x}.txt' for x in todo)}")


def adjudicate(cfg, book, ch):
    source, work = book.source(ch), book.work(ch)
    prompt = render("adjudicate", source_lang=book.lang, book_title=book.title, chapter=ch,
                    style_guide=book.style_guide(), glossary=book.glossary_text(), source=segments.dump(source),
                    draft_a=(work / "draft_a.txt").read_text("utf-8"), draft_b=(work / "draft_b.txt").read_text("utf-8"),
                    notes_a=(work / "notes_a.md").read_text("utf-8"), notes_b=(work / "notes_b.md").read_text("utf-8"),
                    references=book.references_text(ch))
    segs, disagreements = agents.call(cfg, "adjudicator", prompt,
                                      _translation_validator(source, ["seg", "decision", "reason"]))
    segments.save(work / "adjudicated.txt", segs)
    write_json(work / "disagreements.json", seg_sort(disagreements))
    print(f"  裁决完成：{len(disagreements)} 处实质分歧")


def polish(cfg, book, ch, force=False):
    source, work = book.source(ch), book.work(ch)
    adjudicated = segments.load(work / "adjudicated.txt")
    targets = "、".join(dict.fromkeys(r["target"] for r in book.glossary()))
    polished = agents.call(cfg, "polisher", render(
        "polish", book_title=book.title, chapter=ch, style_guide=book.style_guide(),
        glossary_targets=targets or "（暂无）", draft=segments.dump(adjudicated)),
        lambda t: segments.require_same_ids(source, segments.parse(t)))
    changed = segments.changed(adjudicated, polished)
    reverted = []
    if changed:
        items = "\n\n".join(f"[{k}]\n原文：{source[k]}\n润色前：{adjudicated[k]}\n润色后：{polished[k]}" for k in changed)
        verdicts = agents.call(cfg, "adjudicator", render(
            "fidelity_recheck", source_lang=book.lang, book_title=book.title, chapter=ch,
            glossary=book.glossary_text(), items=items), json_list(["seg", "verdict"]))
        ok = {v["seg"] for v in verdicts if str(v["verdict"]).strip().lower() == "ok"}
        reasons = {v["seg"]: v.get("reason", "") for v in verdicts}
        for k in changed:
            if k not in ok:
                reverted.append({"seg": k, "rejected": polished[k], "reason": reasons.get(k, "复查未给出结论，保守回退")})
                polished[k] = adjudicated[k]
    segments.save(work / "polished.txt", polished)
    write_json(work / "recheck.json", {"changed": changed, "reverted": reverted})
    print(f"  润色完成：改动 {len(changed)} 段，其中 {len(reverted)} 段因改变原意被回退")
    final = book.final_path(ch)
    if final.exists() and not force:
        print(f"  {final} 已存在（可能含人工修改），未覆盖。如需覆盖请加 --force")
    else:
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "polished.txt", final)
        print(f"  已生成待审稿：{final}")


def check(book, ch, version="final"):
    issues, stats = evals.mechanical(book, ch, version)
    print(f"  机械检查 [{version}] 第 {ch} 章：完整性 {stats['completeness']:.0%}，"
          f"{stats['errors']} 个错误，{stats['warnings']} 个警告")
    for i in issues:
        print(f"    [{i['seg']}] {i['level']}: {i['msg']}")
    return stats["errors"]


def review(cfg, book, ch):
    source, final, work = book.source(ch), book.version("final", ch), book.work(ch)
    refs = book.references(ch)
    flags = {k: [] for k in source}

    def flag(seg, text, attention=True):
        flags.setdefault(seg, []).append((attention, text))

    for d in read_json(work / "disagreements.json", []):
        conf = {"high": "高", "medium": "中", "low": "低"}.get(str(d.get("confidence", "")).lower(), "?")
        text = (f"**初译分歧**（裁决把握：{conf}）「{d.get('source_phrase', '')}」　甲：{d.get('a', '')}　"
                f"乙：{d.get('b', '')}　英译：{d.get('reference', '') or '—'}　裁决：{d['decision']}。{d['reason']}")
        if d.get("suggested_note"):
            text += f"　*建议译注：{d['suggested_note']}*"
        flag(d["seg"], text, conf != "高")
    for r in read_json(work / "recheck.json", {}).get("reverted", []):
        flag(r["seg"], f"**润色被回退**：润色稿「{r['rejected']}」改变了原意——{r['reason']}", False)
    for i in read_json(work / "check__final.json", {}).get("issues", []):
        flag(i["seg"], f"**机械检查**：{i['msg']}", i["level"] != "info")
    mqm_runs = [r for r in evals._load(book, ch, "mqm__final__*.json")]
    serious = {}
    for r in mqm_runs:
        for i in r["items"]:
            if i["severity"] != "minor":
                serious.setdefault(i["seg"], set()).add(r["judge"])
            flag(i["seg"], f"**评审 {r['judge']}**〔{evals.SEVERITIES[i['severity']]}·{evals.CATEGORIES[i['category']]}〕"
                           f"「{i.get('span', '')}」{i['explanation']}　建议：{i.get('suggestion', '') or '—'}",
                 i["severity"] != "minor")
    for r in evals._load(book, ch, "backtranslation__final.json"):
        for i in r["items"]:
            flag(i["seg"], f"**回译核对**〔{evals.SEVERITIES[i['severity']]}〕{i['explanation']}", i["severity"] != "minor")
    for r in evals._load(book, ch, "qa__final.json"):
        for i in r["items"]:
            if i["grade"] != "correct":
                flag(i["seg"], f"**问答测试**：问「{i['question']}」应答「{i['expected']}」，"
                               f"只看译文的回答是「{i['answer']}」", i["grade"] == "wrong")
    for s in book.scripture(ch):
        flag(s["seg"], f"经文{s['kind']}：{s['ref']}　{s.get('note', '')}", False)

    attention = [k for k in sorted(flags, key=segments.key) if any(a for a, _ in flags[k])]
    both = [k for k, js in serious.items() if len(js) > 1]
    lines = [f"# 《{book.title}》第 {ch} 章 审阅单", "",
             f"共 {len(source)} 段，其中 **{len(attention)} 段需要重点看**：{', '.join(attention) or '无'}", ""]
    if len(mqm_runs) > 1:
        lines += [f"两位评审都指出有重错/致命错的段落：{', '.join(sorted(both, key=segments.key)) or '无'}"
                  "（最值得优先处理；只有一位评审指出的，可能是评审误判，请结合说明判断）", ""]
    lines += ["用法：通读每一段的译文；标有【需看】的段落请对照下面的说明决定是否修改。",
              f"要修改就直接编辑 `{book.final_path(ch).relative_to(ROOT)}`，并在 `decisions.md` 里记一句理由。", ""]
    for k in sorted(source, key=segments.key):
        mark = "【需看】" if k in attention else ""
        lines += [f"## [{k}] {mark}", "", f"**译文**　{final.get(k, '（缺）')}", "", f"**原文**　{source[k]}", ""]
        lines += [f"**{name}**　{r[k]}\n" for name, r in refs.items() if k in r]
        lines += [f"- {text}" for _, text in flags.get(k, [])]
        lines.append("")
    path = work / "review.md"
    path.write_text("\n".join(lines), "utf-8")
    print(f"  审阅单已生成：{path}")


def cmd_approve(args, cfg):
    book = Book(args.book)
    path = book.dir / "reviewed.json"
    reviewed = read_json(path, {})
    for ch in book.parse_chapters(args.ch):
        if check(book, ch):
            sys.exit(f"第 {ch} 章机械检查有错误，未标记为已审")
        reviewed[str(ch)] = datetime.date.today().isoformat()
    write_json(path, reviewed)
    print(f"已标记为人工审阅通过：{args.ch}")


def cmd_build(args, cfg):
    book = Book(args.book)
    reviewed = read_json(book.dir / "reviewed.json", {})
    src = book.meta.get("source", {})
    roles = cfg["roles"]
    out = [f"# {book.title}", ""]
    if book.meta.get("author_zh"):
        out += [book.meta["author_zh"], ""]
    out += ["## 翻译说明", "",
            f"- 底本：{src.get('edition', '')}（{src.get('copyright', '')}）{src.get('url', '')}",
            f"- 本译文由 AI 辅助从{book.lang}原文译出：两个不同的模型（{roles['translator_a']['model']}、"
            f"{roles['translator_b']['model']}）各自独立初译，第三个模型（{roles['adjudicator']['model']}）对照原文裁决合稿，"
            "再经中文润色、自动检查和人工通读。",
            "- 译者不是古典语言或神学专业人士。译文仅供学习交流，难免有错，欢迎指正；引用或研究请核对原文。",
            f"- 授权：{book.meta.get('license', '')}", ""]
    notes, n = [], 0
    for ch in book.chapters():
        if not book.final_path(ch).exists() or (str(ch) not in reviewed and not args.include_unreviewed):
            continue
        final = book.version("final", ch)
        extra = {}
        for s in book.scripture(ch):
            extra.setdefault(s["seg"], []).append(s["ref"] + (f"（化用）" if s["kind"] == "化用" else "")
                                                  + (f"。{s['note']}" if s.get("note") else ""))
        for s in book.notes(ch):
            extra.setdefault(s["seg"], []).append(f"译注：{s['note']}")
        title = book.meta.get("chapter_titles", {}).get(str(ch), f"第 {ch} 章")
        out += [f"## {title}" + ("" if str(ch) in reviewed else "（未经人工审阅）"), ""]
        para = []
        for k in sorted(final, key=segments.key):
            text = f"<sup>{k.split('.')[1]}</sup>{final[k]}"
            if k in extra:
                n += 1
                text += f"[^{n}]"
                notes.append(f"[^{n}]: {ch}:{k.split('.')[1]}　" + "；".join(extra[k]))
            para.append(text)
        out += ["".join(para), ""]
    out += notes
    path = ROOT / "dist" / f"{book.name}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out) + "\n", "utf-8")
    print(f"发布稿已生成：{path}")


def cmd_eval(args, cfg):
    book = Book(args.book)
    for ch in book.parse_chapters(args.ch):
        print(f"评估第 {ch} 章：{args.method}")
        if args.method in ("mqm", "all"):
            evals.mqm(cfg, book, ch, args.version)
        if args.method in ("back", "all"):
            evals.backtranslate(cfg, book, ch, args.version)
        if args.method in ("qa", "all"):
            evals.qa(cfg, book, ch, args.version)
        if args.method in ("mqm", "all"):
            evals.mechanical(book, ch, args.version)
        if args.method == "seed":
            evals.seed(cfg, book, ch, args.version)
        if args.method == "pairwise":
            if not args.against:
                sys.exit("pairwise 需要 --against 指定另一个版本")
            evals.pairwise(cfg, book, ch, args.version, args.against)
        if args.method == "human":
            evals.human(book, ch, args.version, args.against)
    if args.method != "human":
        evals.report(cfg, book, book.chapters())


def cmd_stage(args, cfg):
    book = Book(args.book)
    if args.cmd == "translate":
        failures = []
        for ch in book.parse_chapters(args.ch):
            print(f"第 {ch} 章：translate")
            try:
                translate(cfg, book, ch, args.only or "ab", args.force)
            except (RuntimeError, ValueError) as e:
                print(f"  第 {ch} 章失败：{e}", file=sys.stderr)
                failures.append(ch)
        if failures:
            sys.exit(f"以下章节初译失败，重跑同一命令即可（已完成的会跳过）：{', '.join(map(str, failures))}")
        return
    failed = 0
    for ch in book.parse_chapters(args.ch):
        print(f"第 {ch} 章：{args.cmd}")
        if args.cmd == "run":
            translate(cfg, book, ch, force=args.force)
        if args.cmd in ("adjudicate", "run"):
            adjudicate(cfg, book, ch)
        if args.cmd in ("polish", "run"):
            polish(cfg, book, ch, args.force)
        if args.cmd in ("check", "run"):
            failed += check(book, ch, getattr(args, "version", None) or "final")
        if args.cmd == "run":
            evals.mqm(cfg, book, ch, "final")
        if args.cmd in ("review", "run"):
            if args.cmd == "review":
                evals.mechanical(book, ch, "final")
            review(cfg, book, ch)
    if args.cmd == "run":
        evals.report(cfg, book, book.chapters())
    sys.exit(1 if failed else 0)


def main():
    p = argparse.ArgumentParser(description="基督教公版文献中译工作流")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_text, ch=True):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("book", help="books/ 下的目录名，如 1clement")
        if ch:
            sp.add_argument("--ch", help="章节：5 / 1-3 / 1,4,7 / bench（基准集）/ all（默认）")
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "新建一本书的目录骨架", ch=False)
    add("glossary", cmd_glossary, "译前：抽取术语候选（之后需人工确认锁定）").add_argument("--batch", type=int, default=10)
    add("scripture", cmd_scripture, "译前：标出圣经引用")
    tr = add("translate", cmd_stage, "阶段 1：两个模型独立初译（已有初译的章节自动跳过）")
    tr.add_argument("--only", choices=["a", "b"], help="只运行译者甲（a）或乙（b）")
    tr.add_argument("--force", action="store_true", help="已有初译也重译")
    add("adjudicate", cmd_stage, "阶段 2：第三个模型对照原文裁决合稿")
    add("polish", cmd_stage, "阶段 3：中文润色 + 忠实度复查").add_argument("--force", action="store_true")
    add("check", cmd_stage, "阶段 4：机械检查").add_argument("--version", default="final")
    add("review", cmd_stage, "生成人工审阅单")
    add("run", cmd_stage, "一条龙：初译→裁决→润色→检查→评审→审阅单").add_argument("--force", action="store_true")
    add("approve", cmd_approve, "阶段 5：标记章节已人工审阅通过")
    ev = add("eval", cmd_eval, "评估")
    ev.add_argument("method", choices=["mqm", "back", "qa", "all", "pairwise", "seed", "human"])
    ev.add_argument("--version", default="final", help="要评估的版本：final / draft_a / draft_b / adjudicated / polished / baseline 下的目录名")
    ev.add_argument("--against", help="pairwise / human 对比时的另一个版本")
    add("build", cmd_build, "阶段 6：生成发布稿", ch=False).add_argument("--include-unreviewed", action="store_true")

    args = p.parse_args()
    if not hasattr(args, "force"):
        args.force = False
    args.fn(args, load_config())


if __name__ == "__main__":
    main()
