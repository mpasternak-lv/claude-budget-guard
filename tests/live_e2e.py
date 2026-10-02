"""Live end-to-end checks against the installed Budget Guard plugin.

Drives real headless Claude Code sessions, so it needs Claude Code with the
plugin installed and a signed-in account. It spends a few cents on Haiku.

Headless sessions read your real settings, so the checks change your saved
budget to force each situation. While they run, an override suspends the
limits in your own sessions, and on exit (pass or fail) your saved budget and
your override are put back as they were. The guard state the checks use lives
in a temporary directory.

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
from typing import Any, Dict, List, Optional

PLUGIN_ID = "budget-guard@budget-guard"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
SETTINGS = CLAUDE_DIR / "settings.json"
REAL_GUARD_HOME = Path.home() / ".claude" / "budget-guard"
MODEL = "haiku"
results: List[bool] = []


def find_claude() -> str:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundled = glob.glob(str(Path.home() / ".vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    if bundled:
        return max(bundled, key=lambda p: [int(n) for n in re.findall(r"\d+", p.split("claude-code-")[1])[:3]])
    sys.exit("No claude binary found. Set CLAUDE_BIN.")


CLAUDE = find_claude()
WORK = Path(tempfile.mkdtemp(prefix="budget-guard-e2e-"))
GUARD_HOME = WORK / "guard"
GUARD_HOME.mkdir()
ENV = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}
ENV["BUDGET_GUARD_HOME"] = str(GUARD_HOME)


def installed_script() -> Path:
    listing = json.loads(subprocess.run([CLAUDE, "plugin", "list", "--json"], capture_output=True, text=True).stdout)
    entry = next((p for p in listing if p["id"] == PLUGIN_ID), None)
    if entry is None:
        sys.exit(f"{PLUGIN_ID} is not installed. Install it first (see README).")
    return Path(entry["installPath"]) / "scripts" / "budget_guard.py"


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


def claude(prompt: str, stream: bool = False) -> str:
    args = [CLAUDE, "-p", prompt, "--model", MODEL]
    if stream:
        args += ["--output-format", "stream-json", "--verbose"]
    done = subprocess.run(args, env=ENV, cwd=WORK, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=300)
    return done.stdout + done.stderr


def saved_options() -> Dict[str, Any]:
    configs = json.loads(SETTINGS.read_text()).get("pluginConfigs") or {}
    return dict((configs.get(PLUGIN_ID) or {}).get("options") or {})


def restore_options(original: Dict[str, Any]) -> None:
    data = json.loads(SETTINGS.read_text())
    configs = data.setdefault("pluginConfigs", {})
    if original:
        configs.setdefault(PLUGIN_ID, {})["options"] = original
    elif PLUGIN_ID in configs:
        configs[PLUGIN_ID].pop("options", None)
        if not configs[PLUGIN_ID]:
            del configs[PLUGIN_ID]
        if not configs:
            del data["pluginConfigs"]
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")


def set_budget(monthly: float, multiplier: float = 5) -> None:
    for args in (["set", f"{monthly:.2f}"], ["set", "multiplier", f"{multiplier:g}"]):
        subprocess.run([sys.executable, str(SCRIPT), *args], env=ENV, check=True, capture_output=True)


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


def set_override(minutes: float, env: Optional[Dict[str, str]] = None) -> None:
    subprocess.run([sys.executable, str(SCRIPT), "allow", str(minutes)], env=env or ENV, check=True, capture_output=True)


def run_hook(payload: Dict[str, Any]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps(payload),
                          env=ENV, capture_output=True, text=True, timeout=30)


def stream_events(text: str) -> List[Dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def session_checks() -> None:
    out = claude("budget set 777")
    check("budget set in a session saves the budget", "set to $777" in out and
          saved_options().get("monthly_budget_usd") == 777, out)
    claude("budget set multiplier 3")
    out = claude("budget status")
    check("the changed limits reach the next session", "Limits: $777 a month, with a 5h cap at 3x pace." in out, out)

    claude("Reply with exactly: ok")  # makes sure the 5h window has spend to measure
    set_budget(budget_for_ratio(3.0))
    out = claude("Say hi")
    check("over the 5h cap, a prompt is refused", "limit reached" in out, out)
    check("the refusal states how far over", re.search(r"\$[\d.]+ \(\d+%\) over", out) is not None, out)
    check("the refusal projects the month", "comes to about $" in out, out)
    check("the refusal explains both overrides", "budget override 30" in out and " allow 30" in out, out)

    out = claude("budget status")
    check("budget status works while locked out", "Budget guard status:" in out and "over" in out, out)

    out = claude("budget override 2")
    check("budget override unlocks", "limits suspended until" in out, out)
    out = claude("Reply with exactly: unlocked")
    check("after the override, a prompt goes through", "unlocked" in out.lower(), out)
    out = claude("budget override 0")
    check("budget override 0 cancels", "override cancelled" in out, out)
    out = claude("Say hi")
    check("after cancelling, prompts are refused again", "limit reached" in out, out)

    # Mid-turn: let the prompt through with an override, then end it before the tool call.
    set_override(5)
    state = GUARD_HOME / "state.json"
    before = state.stat().st_mtime_ns
    proc = subprocess.Popen([CLAUDE, "-p", "Use the Bash tool to run: echo hello-from-tool. Then report its output.",
                             "--model", MODEL, "--output-format", "stream-json", "--verbose"],
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

    out = claude("budget set 999999")
    check("raising the budget while locked out applies to the next message",
          "set to $999,999" in out and "limit reached" not in claude("Reply with exactly: ok"), out)

    # 85%, not just past 80%: other sessions keep spending while this runs.
    set_budget(budget_for_ratio(0.85))
    events = stream_events(claude("Reply with exactly: ok", stream=True))
    notices = [json.dumps(e) for e in events if e.get("type") == "system" and "Budget guard" in json.dumps(e)]
    check("near the cap, the prompt runs with a warning", bool(notices) and any(
        e.get("type") == "result" and not e.get("is_error") for e in events), json.dumps(events)[-1500:])

    set_budget(max(live()["month"] - 1, 1))
    out = claude("Say hi")
    check("over the monthly budget, a prompt is refused", "limit reached" in out and "monthly" in out, out)
    check("no projection once the month is spent", "comes to about" not in out, out)


def main() -> int:
    print(f"claude: {CLAUDE}\nplugin script: {SCRIPT}\nscratch: {WORK}\n")

    listing = json.loads(subprocess.run([CLAUDE, "plugin", "list", "--json"], capture_output=True, text=True).stdout)
    check("plugin is installed and enabled", any(p["id"] == PLUGIN_ID and p["enabled"] for p in listing))

    manifest = json.loads((SCRIPT.parent.parent / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    original = saved_options()
    expected = float(original.get("monthly_budget_usd", manifest["monthly_budget_usd"]["default"]))
    out = claude("budget status")
    check(f"budget status shows your configured ${expected:,.0f} budget", f"Limits: ${expected:,.0f} a month" in out, out)

    real_env = {k: v for k, v in ENV.items() if k != "BUDGET_GUARD_HOME"}
    real_override = REAL_GUARD_HOME / "override.json"
    real_before = real_override.read_text() if real_override.exists() else None
    set_override(60, real_env)  # keeps your own sessions working while budgets are forced
    try:
        session_checks()
    finally:
        restore_options(original)
        if real_before is None:
            real_override.unlink(missing_ok=True)
        else:
            real_override.write_text(real_before)
    check("your saved budget is restored", saved_options() == original, f"{saved_options()} != {original}")
    check("your own override is restored", (real_override.read_text() if real_override.exists() else None) == real_before)

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

    (GUARD_HOME / "config.json").write_text("{broken")
    broken = run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "hi"})
    logged = (GUARD_HOME / "errors.log").exists() and "Traceback" in (GUARD_HOME / "errors.log").read_text()
    check("a broken config fails open and is logged", broken.returncode == 0 and not broken.stdout.strip() and logged)

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
