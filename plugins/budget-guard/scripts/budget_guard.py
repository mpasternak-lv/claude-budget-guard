#!/usr/bin/env python3
"""Budget Guard: local spend limits for usage-billed Claude Code.

Prices every assistant message in Claude Code's local session logs
(~/.claude/projects/**/*.jsonl) at API list rates and enforces spend limits
through Claude Code hooks. It works the same in the terminal CLI and the VS
Code extension, since both write the same logs and run the same hooks.

- UserPromptSubmit: refuses a new prompt while any limit is exceeded, and
  shows a warning once spend passes `warn_at` of a limit.
- PreToolUse: denies tool calls while a limit is exceeded, so a long agentic
  turn stops instead of running past the limit.

Limits:
- monthly_usd: hard cap from 00:00 UTC on the 1st, matching Anthropic's own
  monthly spend limit.
- rolling_hours_usd: {"5": "pace"} caps spend in any 5 hours. "pace" means
  pace_multiplier times the spend that a constant rate, 24 hours a day, would
  need over that window to use exactly what is left of monthly_usd by the end
  of the month. A number is a fixed cap in USD.
- daily_usd: the same, per local calendar day ("pace", a number, or null).

monthly_usd and pace_multiplier come from the plugin's settings (/config in
Claude Code). Hooks receive them as CLAUDE_PLUGIN_OPTION_* variables, and the
terminal commands read the same saved values from settings.json. The other
keys, and these two outside the plugin, come from config.json in the state
directory.

The figures are estimates for a circuit breaker, not an invoice. They count
only Claude Code on this machine, not claude.ai chat or other devices, and
they are only as current as PRICES below.

Messages the hook answers itself, without sending them to the model:
  budget status          spend, limits, and the month-end projection
  budget override 30     suspend the limits for 30 minutes (0 cancels)

Usage:
  budget_guard.py hook               # run as a Claude Code hook (stdin JSON)
  budget_guard.py status             # spend against each limit
  budget_guard.py report [DAYS]      # per-day spend by model, default 14 days
  budget_guard.py allow MINUTES      # suspend enforcement for MINUTES
  budget_guard.py allow 0            # cancel an override

Standard library only, Python 3.9+.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Outside the plugin directory, which is replaced on every plugin update.
HOME = Path(os.environ.get("BUDGET_GUARD_HOME", Path.home() / ".claude" / "budget-guard"))
CONFIG_PATH = HOME / "config.json"
STATE_PATH = HOME / "state.json"
# Separate from the state file: every hook run rewrites state.json, and a run
# that read it before an override was saved would write the override away.
OVERRIDE_PATH = HOME / "override.json"
ERROR_LOG = HOME / "errors.log"
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
PLUGIN_NAME = "budget-guard"
# Plugin option key -> config key. Option keys are declared in plugin.json.
PLUGIN_OPTIONS = {"monthly_budget_usd": "monthly_usd", "pace_multiplier": "pace_multiplier"}

# Events older than this are dropped from the state file. It must exceed the
# longest window a limit can use (a calendar month).
RETENTION_DAYS = 40

# USD per million tokens at Anthropic first-party list rates. A model ID is
# matched to the longest key it starts with, so "claude-opus-5-5" must stay
# distinct from "claude-opus-5". "cache_read" is listed per model because it is
# not a fixed fraction of input across models.
PRICES: Dict[str, Dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.00},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-opus-4": {"input": 5.0, "output": 25.0, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10},
}
# Priced at the most expensive tier so an unrecognized model is never free.
UNKNOWN_MODEL_PRICE = PRICES["claude-fable-5"]
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
FAST_MODE_MULTIPLIER = 2.0
WEB_SEARCH_USD_PER_REQUEST = 0.01

DEFAULT_CONFIG: Dict[str, Any] = {
    "monthly_usd": None,
    "daily_usd": None,
    "rolling_hours_usd": {"5": "pace"},
    "pace_multiplier": 5.0,
    "warn_at": 0.8,
    "block_tools_when_over": True,
}

Event = List[Any]  # [epoch seconds, cost USD, model ID]

# A prompt consisting only of this phrase sets an override from inside Claude
# Code, so a locked-out user needs no terminal. The hook blocks that prompt, so
# the phrase never reaches the model or costs anything.
OVERRIDE_PHRASE = re.compile(r"^\s*budget\s+override(?:\s+(\d+))?\s*$", re.IGNORECASE)
STATUS_PHRASE = re.compile(r"^\s*budget\s+status\s*$", re.IGNORECASE)
DEFAULT_OVERRIDE_MINUTES = 30


def plugin_options() -> Dict[str, Any]:
    """The plugin's saved options, keyed by config key.

    Hooks get them in the environment. A terminal command does not, so it reads
    the values Claude Code saved under pluginConfigs in settings.json.
    """
    found: Dict[str, Any] = {}
    for option, key in PLUGIN_OPTIONS.items():
        value = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{option.upper()}")
        if value not in (None, ""):
            found[key] = float(value)
    if not found:
        try:
            configs = json.loads((CLAUDE_DIR / "settings.json").read_text()).get("pluginConfigs") or {}
        except (OSError, ValueError, AttributeError):
            configs = {}
        for plugin_id, entry in configs.items():
            if plugin_id.split("@")[0] == PLUGIN_NAME and isinstance(entry, dict):
                for option, value in (entry.get("options") or {}).items():
                    if option in PLUGIN_OPTIONS and value not in (None, ""):
                        found[PLUGIN_OPTIONS[option]] = float(value)
    # Claude Code exports and saves only values the user has set, not the
    # manifest defaults, so a plugin install fills the gaps from plugin.json.
    for option, default in manifest_defaults().items():
        found.setdefault(PLUGIN_OPTIONS[option], float(default))
    return found


def manifest_defaults() -> Dict[str, Any]:
    """userConfig defaults from plugin.json, or nothing when not run as a plugin."""
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        user_config = json.loads(manifest.read_text()).get("userConfig") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {option: spec["default"] for option, spec in user_config.items()
            if option in PLUGIN_OPTIONS and isinstance(spec, dict) and "default" in spec}


def load_config() -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text()))
    config.update(plugin_options())
    return config


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except ValueError:
            pass
    return {"files": {}, "events": {}}


def write_json(path: Path, data: Dict[str, Any]) -> None:
    # Write-then-rename: concurrent sessions run this hook at the same time,
    # and a reader must never see a half-written file.
    HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HOME, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def save_state(state: Dict[str, Any]) -> None:
    write_json(STATE_PATH, state)


def price_for(model: str) -> Dict[str, float]:
    # Bedrock and Vertex IDs carry a prefix ("us.anthropic.claude-...").
    if "claude-" in model:
        model = model[model.index("claude-"):]
    matches = [key for key in PRICES if model.startswith(key)]
    return PRICES[max(matches, key=len)] if matches else UNKNOWN_MODEL_PRICE


def message_cost(model: str, usage: Dict[str, Any]) -> float:
    rate = price_for(model)
    cache_creation = usage.get("cache_creation") or {}
    write_1h = cache_creation.get("ephemeral_1h_input_tokens")
    write_5m = cache_creation.get("ephemeral_5m_input_tokens")
    if write_1h is None and write_5m is None:
        write_1h, write_5m = 0, usage.get("cache_creation_input_tokens") or 0
    tokens_cost = (
        (usage.get("input_tokens") or 0) * rate["input"]
        + (write_5m or 0) * rate["input"] * CACHE_WRITE_5M_MULTIPLIER
        + (write_1h or 0) * rate["input"] * CACHE_WRITE_1H_MULTIPLIER
        + (usage.get("cache_read_input_tokens") or 0) * rate["cache_read"]
        + (usage.get("output_tokens") or 0) * rate["output"]
    ) / 1_000_000
    if usage.get("speed") == "fast":
        tokens_cost *= FAST_MODE_MULTIPLIER
    searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
    return tokens_cost + searches * WEB_SEARCH_USD_PER_REQUEST


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ingest_file(path: Path, file_state: Dict[str, Any], events: Dict[str, Event]) -> None:
    """Read the lines appended since the last call and record new messages.

    Claude Code writes one line per content block, so a message appears on
    several lines with identical usage. Resumed sessions can copy messages into
    a new file. Keying events by message ID counts each one once either way.
    """
    size = path.stat().st_size
    offset = file_state.get("offset", 0)
    if size < offset:  # file was rewritten
        offset = 0
    if size == offset:
        return
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    # Stop at the last newline: the final line may still be mid-write.
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    for raw in chunk[: end + 1].splitlines():
        if b'"usage"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage, model = message.get("usage"), message.get("model") or ""
        key = message.get("id") or entry.get("requestId") or entry.get("uuid")
        if not usage or not key or model == "<synthetic>" or key in events:
            continue
        try:
            ts = parse_timestamp(entry["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        events[key] = [ts, message_cost(model, usage), model]
    file_state["offset"] = offset + end + 1


def refresh(state: Dict[str, Any]) -> Dict[str, Event]:
    cutoff = time.time() - RETENTION_DAYS * 86400
    files, events = state["files"], state["events"]
    seen = set()
    for root, _dirs, names in os.walk(PROJECTS_DIR):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = Path(root) / name
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                key = str(path)
                seen.add(key)
                ingest_file(path, files.setdefault(key, {}), events)
            except OSError:
                continue
    for key in list(files):
        if key not in seen:
            del files[key]
    for key in [k for k, e in events.items() if e[0] < cutoff]:
        del events[key]
    return events


def spend_between(events: Dict[str, Event], since: float, until: float) -> float:
    return sum(e[1] for e in events.values() if since <= e[0] < until)


def month_bounds_utc(now: float) -> Tuple[float, float]:
    start = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = calendar.monthrange(start.year, start.month)[1]
    return start.timestamp(), (start + timedelta(days=days)).timestamp()


def paced_limit(config: Dict[str, Any], events: Dict[str, Event], window_start: float,
                window_hours: float, month: Tuple[float, float]) -> float:
    """Spend allowed in a window under the pace rule.

    The rate is fixed at the window's start: what is left of monthly_usd then,
    spread evenly over the hours from then to the end of the month. Fixing it
    at the start stops spend inside the window from shrinking its own limit,
    and keeps at least one window's length in the divisor, so the limit stays
    bounded in the last hours of the month.
    """
    month_start, month_end = month
    anchor = max(window_start, month_start)
    remaining = max(float(config["monthly_usd"]) - spend_between(events, month_start, anchor), 0.0)
    hours_left = (month_end - anchor) / 3600
    return float(config.get("pace_multiplier", DEFAULT_CONFIG["pace_multiplier"])) * window_hours * remaining / hours_left


def frees_at(events: Dict[str, Event], since: float, until: float, limit: float, window_s: float) -> Optional[float]:
    """When enough of the window's spend ages out to fall back under limit."""
    in_window = sorted((e[0], e[1]) for e in events.values() if since <= e[0] < until)
    spent = sum(cost for _ts, cost in in_window)
    for ts, cost in in_window:
        if spent < limit:
            break
        spent -= cost
        if spent < limit:
            return ts + window_s
    return None


def clock(ts: float) -> str:
    local = datetime.fromtimestamp(ts).astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%b %d %H:%M")


def evaluate(config: Dict[str, Any], events: Dict[str, Event], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Return one row per configured limit: name, spent, limit, resets."""
    now = time.time() if now is None else now
    month = month_bounds_utc(now)
    monthly = config.get("monthly_usd")
    rows = []

    if monthly is not None:
        rows.append({"name": "monthly", "spent": spend_between(events, month[0], now + 1),
                     "limit": float(monthly), "resets": clock(month[1])})

    daily = config.get("daily_usd")
    if daily is not None:
        local_now = datetime.fromtimestamp(now).astimezone()
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if daily == "pace":
            limit = paced_limit(config, events, day_start, 24.0, month) if monthly is not None else None
        else:
            limit = float(daily)
        if limit is not None:
            rows.append({"name": "daily", "spent": spend_between(events, day_start, now + 1),
                         "limit": limit, "resets": "at midnight", "paced": daily == "pace"})

    for hours, value in sorted((config.get("rolling_hours_usd") or {}).items(), key=lambda kv: float(kv[0])):
        window_s = float(hours) * 3600
        since = now - window_s
        if value == "pace":
            if monthly is None:
                continue
            limit = paced_limit(config, events, since, float(hours), month)
        else:
            limit = float(value)
        spent = spend_between(events, since, now + 1)
        free = frees_at(events, since, now + 1, limit, window_s)
        rows.append({"name": f"{hours}h", "spent": spent, "limit": limit, "paced": value == "pace",
                     "resets": f"rolling, under the limit again at {clock(free)}" if free else "rolling"})
    return rows


def describe(row: Dict[str, Any]) -> str:
    spent, limit = row["spent"], row["limit"]
    text = f"{row['name']} ${spent:.2f} of ${limit:.2f}"
    if spent > limit:
        excess = f"${spent - limit:.2f}"
        text += f", {excess} ({100 * (spent - limit) / limit:.0f}%) over" if limit > 0 else f", {excess} over"
    return f"{text} ({row['resets']})"


def projection(config: Dict[str, Any], rows: List[Dict[str, Any]], now: Optional[float] = None) -> Optional[str]:
    """Month-end spend if the most intense paced window's rate continues.

    A paced limit is the spend that lands the month exactly on monthly_usd, so
    spending r times a paced limit projects r times the budget still left.
    """
    monthly = next((r for r in rows if r["name"] == "monthly"), None)
    paced = [r for r in rows if r.get("paced") and r["limit"] > 0]
    if monthly is None or not paced or monthly["spent"] >= monthly["limit"]:
        return None
    window = max(paced, key=lambda r: r["spent"] / r["limit"])
    ratio = window["spent"] / window["limit"]
    if ratio == 0:  # an idle window says nothing about the rest of the month
        return None
    projected = monthly["spent"] + ratio * (monthly["limit"] - monthly["spent"])
    month = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).strftime("%B")
    return (f"If you keep up the last {window['name']}'s pace whenever you work, {month} comes to "
            f"about ${projected:,.0f} against your ${monthly['limit']:,.0f} budget.")


def override_until() -> float:
    try:
        return float(json.loads(OVERRIDE_PATH.read_text()).get("until", 0))
    except (OSError, ValueError, AttributeError):
        return 0.0


def override_active() -> bool:
    return time.time() < override_until()


def set_override(minutes: float) -> str:
    until = time.time() + minutes * 60 if minutes > 0 else 0
    write_json(OVERRIDE_PATH, {"until": until})
    if minutes > 0:
        return f"limits suspended until {clock(until)}"
    return "override cancelled, limits are enforced again"


def override_help() -> str:
    terminal = f"python3 {Path(__file__).resolve()} allow {DEFAULT_OVERRIDE_MINUTES}"
    return (
        f"To keep working for {DEFAULT_OVERRIDE_MINUTES} minutes, either send this as a message: "
        f"budget override {DEFAULT_OVERRIDE_MINUTES}  (or run in a terminal: {terminal})"
    )


def run_hook() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    event_name = payload.get("hook_event_name")
    config = load_config()
    state = load_state()
    events = refresh(state)

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        if STATUS_PHRASE.match(prompt):
            save_state(state)
            print(json.dumps({"decision": "block", "reason": "Budget guard status:\n" + status_text(config, events)}))
            return 0
        command = OVERRIDE_PHRASE.match(prompt)
        if command:
            minutes = float(command.group(1) or DEFAULT_OVERRIDE_MINUTES)
            result = set_override(minutes)
            again = " Send your prompt again." if minutes > 0 else ""
            print(json.dumps({"decision": "block", "reason": f"Budget guard: {result}.{again}"}))
            return 0

    save_state(state)
    if override_active():
        return 0
    rows = evaluate(config, events)
    over = [r for r in rows if r["spent"] >= r["limit"]]
    forecast = projection(config, rows)
    summary = "; ".join(describe(r) for r in over) + "." + (f" {forecast}" if forecast else "")

    if event_name == "UserPromptSubmit":
        if over:
            reason = f"Budget guard: limit reached. {summary} {override_help()}"
            print(json.dumps({"decision": "block", "reason": reason}))
            return 0
        warn_at = float(config.get("warn_at", 0.8))
        near = [r for r in rows if r["limit"] > 0 and r["spent"] >= warn_at * r["limit"]]
        if near:
            message = "Budget guard: " + "; ".join(describe(r) for r in near) + "."
            print(json.dumps({"systemMessage": message + (f" {forecast}" if forecast else "")}))
        return 0

    if event_name == "PreToolUse" and over and config.get("block_tools_when_over", True):
        print(json.dumps({
            "systemMessage": f"Budget guard: limit reached mid-turn. {summary} {override_help()}",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"The user's own Claude spend limit is reached. {summary} Do not call any more tools "
                    "and do not retry this one. Stop now and tell the user what is done, what is half-done, "
                    "and what remains. Then tell them how to continue, quoting this exactly: "
                    f"\"{override_help()}\". After that they can ask you to resume."
                ),
            },
        }))
    return 0


def status_text(config: Dict[str, Any], events: Dict[str, Event]) -> str:
    rows = evaluate(config, events)
    lines = [f"{describe(row)}  {100 * row['spent'] / row['limit'] if row['limit'] else 100:.0f}%" for row in rows]
    forecast = projection(config, rows)
    if forecast:
        lines.append(forecast)
    if override_active():
        lines.append(f"Override active until {clock(override_until())}: limits are not enforced.")
    if config.get("monthly_usd") is None:
        lines.append("No monthly budget is set.")
    else:
        lines.append(f"Limits: ${float(config['monthly_usd']):,.0f} a month, with a 5h cap at "
                     f"{float(config['pace_multiplier']):g}x pace.")
    lines.append("Change them in Claude Code with /config (under Budget Guard) or /plugin configure "
                 "budget-guard@budget-guard, then start a new session.")
    return "\n".join(lines)


def cmd_status() -> int:
    config = load_config()
    state = load_state()
    events = refresh(state)
    save_state(state)
    print(status_text(config, events))
    return 0


def cmd_report(days: int) -> int:
    state = load_state()
    events = refresh(state)
    save_state(state)
    since = time.time() - days * 86400
    by_day: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for ts, cost, model in events.values():
        if ts >= since:
            by_day[datetime.fromtimestamp(ts).strftime("%a %Y-%m-%d")][model] += cost
    total = 0.0
    for day in sorted(by_day, key=lambda d: d[4:]):
        models = by_day[day]
        day_total = sum(models.values())
        total += day_total
        detail = ", ".join(f"{m} ${c:.2f}" for m, c in sorted(models.items(), key=lambda kv: -kv[1]))
        print(f"{day}  ${day_total:8.2f}  {detail}")
    print(f"Total over {days} days: ${total:.2f}")
    return 0


def cmd_allow(minutes: float) -> int:
    result = set_override(minutes)
    print(result[0].upper() + result[1:] + ".")
    return 0


def main(argv: List[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    if command == "hook":
        try:
            return run_hook()
        except Exception:
            # A broken guard must not block Claude Code: log and allow.
            HOME.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a") as f:
                f.write(f"{datetime.now().isoformat()}\n{traceback.format_exc()}\n")
            return 0
    if command == "status":
        return cmd_status()
    if command == "report":
        return cmd_report(int(argv[2]) if len(argv) > 2 else 14)
    if command == "allow" and len(argv) > 2:
        return cmd_allow(float(argv[2]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
