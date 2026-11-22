import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
ISOLATION = ROOT / "config" / "devin_isolation.json"
HOOK_LOG = "__HOOK_LOG__"
NO_TOOLS = ("\n\n（请直接输出结果。不要使用任何工具，不要读写文件，不要上网搜索。"
            "只输出一次最终结果，不要先输出再修改。"
            "如果要输出 JSON，字符串里需要引号时请用中文引号“”或「」，不要用英文双引号。）\n")


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def isolation_template():
    text = ISOLATION.read_text("utf-8")
    if HOOK_LOG not in text or "exit 2" not in text:
        raise RuntimeError(f"{ISOLATION} 缺少拦截 hook，拒绝在无隔离的情况下调用 devin")
    return text


def devin_version():
    """每次调用前重新读取：devin 会在后台自动升级，下一次启动即换用新版本。"""
    out = subprocess.run(["devin", "version"], capture_output=True, text=True, timeout=60).stdout.strip()
    return out.split()[1] if out.startswith("devin ") else out


def _command(role, prompt_file, prompt, config_file=None):
    if role["agent"] == "devin":
        return ["devin", "--model", role["model"], "--config", str(config_file), "--permission-mode", "auto",
                "--respect-workspace-trust", "false", "--verbosity", "0", "--prompt-file", str(prompt_file), "-p"]
    if role["agent"] == "pi":
        cmd = ["pi", "-p", "--no-tools", "--no-session", "--no-context-files", "--no-skills"]
        if role.get("provider"):
            cmd += ["--provider", role["provider"]]
        return cmd + ["--model", role["model"], prompt]
    raise ValueError(f"未知的 agent: {role['agent']}")


def parse_hook_log(text):
    decoder, calls, i = json.JSONDecoder(), [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return calls
        try:
            obj, i = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            calls.append({"tool_name": "?", "raw": text[i:i + 200]})
            return calls
        calls.append(obj)


def strip_banner(text):
    lines = text.splitlines(keepends=True)
    while lines and (not lines[0].strip() or lines[0].strip().startswith("✓")):
        lines.pop(0)
    return "".join(lines)


def _run(role, prompt, timeout):
    """返回 (输出文本, 被拦截的工具调用列表)。devin 调用一律带隔离配置，被 hook 拦截的调用记录在日志里。"""
    with tempfile.TemporaryDirectory(prefix="tw_") as tmp:
        prompt_file = Path(tmp) / "prompt.md"
        prompt_file.write_text(prompt, "utf-8")
        workdir = Path(tmp) / "empty"
        workdir.mkdir()
        config_file, log = None, Path(tmp) / "hook.log"
        if role["agent"] == "devin":
            config_file = Path(tmp) / "devin_config.json"
            config_file.write_text(isolation_template().replace(HOOK_LOG, str(log)), "utf-8")
        proc = subprocess.run(_command(role, prompt_file, prompt, config_file), cwd=workdir, capture_output=True,
                              text=True, timeout=timeout)
        calls = parse_hook_log(log.read_text("utf-8")) if log.exists() else []
    if proc.returncode != 0:
        raise RuntimeError(f"{role['agent']} 退出码 {proc.returncode}: {proc.stderr.strip()[-500:]}")
    text = strip_banner(proc.stdout)
    if not text.strip():
        raise RuntimeError(f"{role['agent']} 没有输出: {proc.stderr.strip()[-500:]}")
    return text, calls


def label(role):
    agent = f"pi[{role['provider']}]" if role["agent"] == "pi" and role.get("provider") else role["agent"]
    return f"{agent} / {role['model']}"


def identity(role):
    ident = f"{role['agent']}|{role.get('provider', '')}|{role['model']}"
    if role["agent"] == "devin":
        ident += f"|isolation={sha256(isolation_template())[:16]}|cli={devin_version()}"
    return ident


def cache_path(role, prompt):
    base = f"{role['agent']}|{role.get('provider', '')}|{role['model']}"
    return CACHE / base.replace("|", "_").replace("/", "_") / f"{sha256(identity(role) + chr(10) + prompt)}.txt"


def call(cfg, role_name, prompt, validate=None, info=None):
    """调用模型并缓存。info 若为 dict，会填入本次调用的来源信息（命中缓存时取自原始调用）。"""
    role = cfg["roles"][role_name]
    opts = cfg.get("agents", {})
    path = cache_path(role, prompt)
    meta_path = path.with_suffix(".meta.json")
    prompt += NO_TOOLS
    if path.exists():
        text = path.read_text("utf-8")
        if info is not None:
            info.update(json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else {"label": label(role)})
            info["cache_hit"] = True
        return validate(text) if validate else text
    error = None
    for attempt in range(1, opts.get("attempts", 2) + 1):
        print(f"  -> {role_name}（{label(role)}）第 {attempt} 次调用…", file=sys.stderr, flush=True)
        text, start = "", time.time()
        try:
            text, calls = _run(role, prompt, opts.get("timeout_seconds", 900))
            elapsed = time.time() - start
            blocked = f"，拦截工具调用 {len(calls)} 次" if calls else ""
            print(f"     完成：{elapsed:.0f} 秒，输入 {len(prompt)} 字符，输出 {len(text)} 字符{blocked}",
                  file=sys.stderr, flush=True)
            result = validate(text) if validate else text
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            error = e
            if isinstance(e, ValueError):
                failed = CACHE / "failed" / f"{role_name}_{path.stem[:12]}.txt"
                failed.parent.mkdir(parents=True, exist_ok=True)
                failed.write_text(text, "utf-8")
            print(f"     失败: {e}", file=sys.stderr)
            continue
        meta = {
            "label": label(role), "agent": role["agent"], "provider": role.get("provider", ""), "model": role["model"],
            "devin_cli_version": devin_version() if role["agent"] == "devin" else None,
            "isolation_sha256": sha256(isolation_template()) if role["agent"] == "devin" else None,
            "called_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": round(elapsed, 1), "attempt": attempt,
            "blocked_tool_calls": [c.get("tool_name", "?") for c in calls],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
        if info is not None:
            info.update(meta, cache_hit=False)
        return result
    raise RuntimeError(f"{role_name} 调用失败: {error}")
