"""Live end-to-end checks against the installed Budget Guard plugin.

Drives real headless Claude Code sessions, so it needs Claude Code with the
plugin installed and a signed-in account. It spends a few cents on Haiku. It
never changes your settings or your override: limits are passed per session
with --settings, and the guard's state lives in a temporary directory.

    python3 tests/live_e2e.py            # CLAUDE_BIN overrides the claude binary

Not part of the unit suite (the file name does not start with test_).
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PLUGIN_ID = "budget-guard@budget-guard"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
MODEL = "haiku"
results: List[bool] = []


def find_claude() -> str:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundled = sorted(glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    if bundled:
        return bundled[-1]
    sys.exit("No claude binary found. Set CLAUDE_BIN.")


CLAUDE = find_claude()
WORK = Path(tempfile.mkdtemp(prefix="budget-guard-e2e-"))
GUARD_HOME = WORK / "guard"
GUARD_HOME.mkdir()
ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}
ENV["BUDGET_GUARD_HOME"] = str(GUARD_HOME)


def installed_script() -> Path:
    candidates = glob.glob(str(CLAUDE_DIR / "plugins/cache/budget-guard/budget-guard/*/scripts/budget_guard.py"))
    if not candidates:
        sys.exit(f"{PLUGIN_ID} is not installed. Install it first (see README).")
    return Path(max(candidates, key=os.path.getmtime))


SCRIPT = installed_script()


def load_guard() -> Any:
    os.environ["BUDGET_GUARD_HOME"] = str(GUARD_HOME)
    spec = importlib.util.spec_from_file_location("installed_guard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


bg = load_guard()


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail and not ok else ""))


def settings(monthly: float, multiplier: float = 5) -> str:
    return json.dumps({"pluginConfigs": {PLUGIN_ID: {"options": {
        "monthly_budget_usd": monthly, "pace_multiplier": multiplier}}}})


def claude(prompt: str, config: Optional[str] = None, stream: bool = False) -> str:
    args = [CLAUDE, "-p", prompt, "--model", MODEL]
    if config:
        args += ["--settings", config]
    if stream:
        args += ["--output-format", "stream-json", "--verbose"]
    done = subprocess.run(args, env=ENV, cwd=WORK, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=300)
    return done.stdout + done.stderr


def live() -> Dict[str, float]:
    """This month's spend, the spend before the current 5h window, and the hours it has left."""
    events = bg.refresh(bg.load_state())
    now = time.time()
    month_start, month_end = bg.month_bounds_utc(now)
    window_start = max(now - 5 * 3600, month_start)
    return {
        "month": bg.spend_between(events, month_start, now + 1),
        "before_window": bg.spend_between(events, month_start, window_start),
        "window": bg.spend_between(events, now - 5 * 3600, now + 1),
        "hours_left": (month_end - window_start) / 3600,
    }


def budget_for_ratio(ratio: float, multiplier: float = 5) -> float:
    """A monthly budget that puts the current 5h window at `ratio` of its paced cap."""
    s = live()
    cap = s["window"] / ratio
    return s["before_window"] + cap * s["hours_left"] / (5 * multiplier)


def set_override(minutes: float) -> None:
    subprocess.run([sys.executable, str(SCRIPT), "allow", str(minutes)], env=ENV, check=True, capture_output=True)


def run_hook(payload: Dict[str, Any], extra_env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps(payload),
                          env=dict(ENV, **(extra_env or {})), capture_output=True, text=True, timeout=30)


def stream_events(text: str) -> List[Dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def main() -> int:
    print(f"claude: {CLAUDE}\nplugin script: {SCRIPT}\nscratch: {WORK}\n")

    listing = subprocess.run([CLAUDE, "plugin", "list"], capture_output=True, text=True).stdout
    check("plugin is installed and enabled", PLUGIN_ID in listing and "enabled" in listing.split(PLUGIN_ID)[1][:200])

    # Saved values, or the manifest defaults when none are saved.
    manifest = json.loads((SCRIPT.parent.parent / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    saved = ((json.loads((CLAUDE_DIR / "settings.json").read_text()).get("pluginConfigs") or {})
             .get(PLUGIN_ID, {}).get("options", {}))
    expected = float(saved.get("monthly_budget_usd", manifest["monthly_budget_usd"]["default"]))
    out = claude("budget status")
    check(f"budget status shows your configured ${expected:,.0f} budget", f"Limits: ${expected:,.0f} a month" in out, out)

    out = claude("budget status", settings(777, 3))
    check("a changed budget reaches the hook", "Limits: $777 a month, with a 5h cap at 3x pace." in out, out)

    claude("Reply with exactly: ok")  # makes sure the 5h window has spend to measure
    locked = settings(budget_for_ratio(3.0))
    out = claude("Say hi", locked)
    check("over the 5h cap, a prompt is refused", "limit reached" in out, out)
    check("the refusal states how far over", re.search(r"\$[\d.]+ \(\d+%\) over", out) is not None, out)
    check("the refusal projects the month", "comes to about $" in out, out)
    check("the refusal explains both overrides", "budget override 30" in out and " allow 30" in out, out)

    out = claude("budget status", locked)
    check("budget status works while locked out", "Budget guard status:" in out and "over" in out, out)

    out = claude("budget override 2", locked)
    check("budget override unlocks", "limits suspended until" in out, out)
    out = claude("Reply with exactly: unlocked", locked)
    check("after the override, a prompt goes through", "unlocked" in out.lower(), out)
    out = claude("budget override 0", locked)
    check("budget override 0 cancels", "override cancelled" in out, out)
    out = claude("Say hi", locked)
    check("after cancelling, prompts are refused again", "limit reached" in out, out)

    # Mid-turn: let the prompt through with an override, then end it before the tool call.
    set_override(5)
    state = GUARD_HOME / "state.json"
    before = state.stat().st_mtime_ns
    proc = subprocess.Popen([CLAUDE, "-p", "Use the Bash tool to run: echo hello-from-tool. Then report its output.",
                             "--model", MODEL, "--settings", locked, "--output-format", "stream-json", "--verbose"],
                            env=ENV, cwd=WORK, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 60
    while state.stat().st_mtime_ns == before and time.time() < deadline:
        time.sleep(0.02)
    set_override(0)
    text = proc.communicate(timeout=300)[0]
    events = stream_events(text)
    tool_results = [str(c.get("content")) for e in events if e.get("type") == "user"
                    for c in e["message"]["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
    replies = " ".join(c["text"] for e in events if e.get("type") == "assistant"
                       for c in e["message"]["content"] if c.get("type") == "text")
    check("mid-turn, the tool call is denied", any("spend limit is reached" in r for r in tool_results), text[-1500:])
    check("the tool never ran", bool(tool_results) and not any("hello-from-tool" in r for r in tool_results),
          text[-1500:])
    check("Claude stops and passes on the override", "budget override" in replies, replies)

    # 85%, not just past 80%: other sessions keep spending while this runs.
    warn = settings(budget_for_ratio(0.85))
    events = stream_events(claude("Reply with exactly: ok", warn, stream=True))
    notices = [json.dumps(e) for e in events if e.get("type") == "system" and "Budget guard" in json.dumps(e)]
    check("near the cap, the prompt runs with a warning", bool(notices) and any(
        e.get("type") == "result" and not e.get("is_error") for e in events), json.dumps(events)[-1500:])

    out = claude("Say hi", settings(max(live()["month"] - 1, 1)))
    check("over the monthly budget, a prompt is refused", "limit reached" in out and "monthly" in out, out)
    check("no projection once the month is spent", "comes to about" not in out, out)

    # The installed hook, run directly.
    for _ in range(3):
        run_hook({"hook_event_name": "PreToolUse"})
    timings = []
    for _ in range(30):
        start = time.perf_counter()
        run_hook({"hook_event_name": "PreToolUse"})
        timings.append(time.perf_counter() - start)
    p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
    check(f"hook latency p50 {statistics.median(timings) * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms (under 1 s)", p95 < 1.0)

    lost = 0
    for _ in range(10):
        busy = [subprocess.Popen([sys.executable, str(SCRIPT), "hook"], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, env=ENV, text=True) for _ in range(6)]
        for p in busy:
            p.stdin.write(json.dumps({"hook_event_name": "PreToolUse"}))
            p.stdin.close()
        run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "budget override 60"})
        for p in busy:
            p.wait(timeout=30)
        if bg.override_until() <= time.time():
            lost += 1
        set_override(0)
    check(f"an override survives busy concurrent sessions ({lost} of 10 lost)", lost == 0)

    broken = run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"},
                      {"CLAUDE_PLUGIN_OPTION_MONTHLY_BUDGET_USD": "not-a-number"})
    logged = (GUARD_HOME / "errors.log").exists() and "Traceback" in (GUARD_HOME / "errors.log").read_text()
    check("a broken setting fails open and is logged", broken.returncode == 0 and not broken.stdout.strip() and logged)

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
