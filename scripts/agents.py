import hashlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache"
NO_TOOLS = ("\n\n（请直接输出结果。不要使用任何工具，不要读写文件，不要上网搜索。"
            "只输出一次最终结果，不要先输出再修改。"
            "如果要输出 JSON，字符串里需要引号时请用中文引号“”或「」，不要用英文双引号。）\n")


def _command(role, prompt_file, prompt):
    if role["agent"] == "devin":
        return ["devin", "-p", "--model", role["model"], "--respect-workspace-trust", "false",
                "--verbosity", "0", "--prompt-file", str(prompt_file)]
    if role["agent"] == "pi":
        cmd = ["pi", "-p", "--no-tools", "--no-session", "--no-context-files", "--no-skills"]
        if role.get("provider"):
            cmd += ["--provider", role["provider"]]
        return cmd + ["--model", role["model"], prompt]
    raise ValueError(f"未知的 agent: {role['agent']}")


def _run(role, prompt, timeout):
    with tempfile.TemporaryDirectory(prefix="tw_") as tmp:
        prompt_file = Path(tmp) / "prompt.md"
        prompt_file.write_text(prompt, "utf-8")
        workdir = Path(tmp) / "empty"
        workdir.mkdir()
        proc = subprocess.run(_command(role, prompt_file, prompt), cwd=workdir, capture_output=True,
                              text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{role['agent']} 退出码 {proc.returncode}: {proc.stderr.strip()[-500:]}")
    if not proc.stdout.strip():
        raise RuntimeError(f"{role['agent']} 没有输出: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def label(role):
    agent = f"pi[{role['provider']}]" if role["agent"] == "pi" and role.get("provider") else role["agent"]
    return f"{agent} / {role['model']}"


def call(cfg, role_name, prompt, validate=None):
    role = cfg["roles"][role_name]
    opts = cfg.get("agents", {})
    ident = f"{role['agent']}|{role.get('provider', '')}|{role['model']}"
    digest = hashlib.sha256(f"{ident}\n{prompt}".encode()).hexdigest()
    prompt += NO_TOOLS
    path = CACHE / ident.replace("|", "_").replace("/", "_") / f"{digest}.txt"
    if path.exists():
        text = path.read_text("utf-8")
        return validate(text) if validate else text
    error = None
    for attempt in range(1, opts.get("attempts", 2) + 1):
        print(f"  -> {role_name}（{label(role)}）第 {attempt} 次调用…", file=sys.stderr, flush=True)
        text, start = "", time.time()
        try:
            text = _run(role, prompt, opts.get("timeout_seconds", 900))
            print(f"     完成：{time.time() - start:.0f} 秒，输入 {len(prompt)} 字符，输出 {len(text)} 字符",
                  file=sys.stderr, flush=True)
            result = validate(text) if validate else text
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            error = e
            if isinstance(e, ValueError):
                failed = CACHE / "failed" / f"{role_name}_{digest[:12]}.txt"
                failed.parent.mkdir(parents=True, exist_ok=True)
                failed.write_text(text, "utf-8")
            print(f"     失败: {e}", file=sys.stderr)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
        return result
    raise RuntimeError(f"{role_name} 调用失败: {error}")
