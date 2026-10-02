"""Tests for budget_guard.py. Run from the repo root: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "budget-guard" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import budget_guard as bg  # noqa: E402

SCRIPT = SCRIPTS / "budget_guard.py"


def utc(*args: int) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def line(msg_id: str, ts: float, usage: Dict[str, Any], model: str = "claude-haiku-4-5",
         kind: str = "assistant", block: str = "text") -> str:
    return json.dumps({
        "type": kind,
        "timestamp": iso(ts),
        "requestId": "req_" + msg_id,
        "message": {"id": msg_id, "model": model, "usage": usage, "content": [{"type": block}]},
    }) + "\n"


def dollars(amount: float) -> Dict[str, Any]:
    """Usage that costs exactly `amount` USD on claude-haiku-4-5 ($5 per MTok output)."""
    return {"output_tokens": round(amount / 5 * 1_000_000)}


def ev(ts: float, cost: float) -> List[Any]:
    return [ts, cost, "m"]


class PricingTest(unittest.TestCase):
    def test_real_opus_5_5_message(self) -> None:
        # Usage block copied from a Claude Code 2.1.284 session log.
        usage = {
            "input_tokens": 2, "cache_creation_input_tokens": 51006, "cache_read_input_tokens": 26409,
            "output_tokens": 134, "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "cache_creation": {"ephemeral_1h_input_tokens": 51006, "ephemeral_5m_input_tokens": 0},
            "speed": "standard",
        }
        expected = (2 * 4 + 51006 * 4 * 2 + 26409 * 0.20 + 134 * 20) / 1e6
        self.assertAlmostEqual(bg.message_cost("claude-opus-5-5", usage), expected, places=9)

    def test_longest_prefix_wins(self) -> None:
        self.assertEqual(bg.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(bg.price_for("claude-opus-5")["input"], 5.0)
        self.assertEqual(bg.price_for("claude-opus-5-20260601")["input"], 5.0)
        self.assertEqual(bg.price_for("claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(bg.price_for("claude-fable-5")["cache_read"], 1.00)

    def test_provider_prefixed_ids(self) -> None:
        self.assertEqual(bg.price_for("us.anthropic.claude-sonnet-5-5")["input"], 2.0)
        self.assertEqual(bg.price_for("anthropic.claude-haiku-4-5")["output"], 5.0)

    def test_unknown_model_is_priced_at_the_top_tier(self) -> None:
        self.assertEqual(bg.price_for("some-new-model"), bg.UNKNOWN_MODEL_PRICE)
        self.assertEqual(bg.price_for(""), bg.UNKNOWN_MODEL_PRICE)

    def test_cache_write_without_ttl_breakdown_is_priced_as_5m(self) -> None:
        cost = bg.message_cost("claude-haiku-4-5", {"cache_creation_input_tokens": 1_000_000})
        self.assertAlmostEqual(cost, 1.25)

    def test_cache_write_ttls(self) -> None:
        usage = {"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 1_000_000}}
        self.assertAlmostEqual(bg.message_cost("claude-haiku-4-5", usage), 1.25 + 2.0)

    def test_fast_mode_and_web_search(self) -> None:
        usage = {"output_tokens": 1_000_000, "speed": "fast", "server_tool_use": {"web_search_requests": 3}}
        self.assertAlmostEqual(bg.message_cost("claude-opus-5-5", usage), 40.0 + 0.03)

    def test_null_fields_are_zero(self) -> None:
        usage = {"input_tokens": None, "output_tokens": None, "cache_creation": None, "server_tool_use": None}
        self.assertEqual(bg.message_cost("claude-haiku-4-5", usage), 0.0)


class IngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.now = time.time()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, text: str, mode: str = "w") -> Path:
        path = self.dir / name
        with path.open(mode) as f:
            f.write(text)
        return path

    def test_lines_of_one_message_count_once(self) -> None:
        text = "".join(line("m1", self.now, dollars(1), block=b) for b in ("thinking", "text", "tool_use"))
        events: Dict[str, Any] = {}
        bg.ingest_file(self.write("a.jsonl", text), {}, events)
        self.assertEqual(list(events), ["m1"])
        self.assertAlmostEqual(events["m1"][1], 1.0)

    def test_message_copied_into_a_resumed_session_counts_once(self) -> None:
        events: Dict[str, Any] = {}
        bg.ingest_file(self.write("a.jsonl", line("m1", self.now, dollars(1))), {}, events)
        bg.ingest_file(self.write("b.jsonl", line("m1", self.now, dollars(1)) + line("m2", self.now, dollars(2))), {}, events)
        self.assertEqual(sorted(events), ["m1", "m2"])

    def test_partial_last_line_waits_for_its_newline(self) -> None:
        full = line("m2", self.now, dollars(2))
        path = self.write("a.jsonl", line("m1", self.now, dollars(1)) + full[:40])
        state: Dict[str, Any] = {}
        events: Dict[str, Any] = {}
        bg.ingest_file(path, state, events)
        self.assertEqual(list(events), ["m1"])
        self.write("a.jsonl", full[40:], mode="a")
        bg.ingest_file(path, state, events)
        self.assertEqual(sorted(events), ["m1", "m2"])
        self.assertEqual(state["offset"], path.stat().st_size)

    def test_only_appended_bytes_are_read(self) -> None:
        path = self.write("a.jsonl", line("m1", self.now, dollars(1)))
        state: Dict[str, Any] = {}
        events: Dict[str, Any] = {}
        bg.ingest_file(path, state, events)
        del events["m1"]  # a full re-read would add it back
        self.write("a.jsonl", line("m2", self.now, dollars(2)), mode="a")
        bg.ingest_file(path, state, events)
        self.assertEqual(list(events), ["m2"])

    def test_rewritten_file_is_reread_from_the_start(self) -> None:
        path = self.write("a.jsonl", line("m1", self.now, dollars(1)) + line("m2", self.now, dollars(1)))
        state: Dict[str, Any] = {}
        events: Dict[str, Any] = {}
        bg.ingest_file(path, state, events)
        self.write("a.jsonl", line("m3", self.now, dollars(3)))
        bg.ingest_file(path, state, events)
        self.assertIn("m3", events)

    def test_skips_lines_that_are_not_billable_messages(self) -> None:
        text = (
            line("u1", self.now, dollars(1), kind="user")
            + line("s1", self.now, dollars(1), model="<synthetic>")
            + "{not json with \"usage\"\n"
            + json.dumps({"type": "assistant", "message": {"id": "x", "model": "m", "usage": {"output_tokens": 1}}}) + "\n"
            + json.dumps({"type": "assistant", "timestamp": iso(self.now), "message": {"id": "y", "usage": {}}}) + "\n"
            + line("ok", self.now, dollars(1))
        )
        events: Dict[str, Any] = {}
        bg.ingest_file(self.write("a.jsonl", text), {}, events)
        self.assertEqual(list(events), ["ok"])


class RefreshTest(unittest.TestCase):
    def test_walks_subagent_logs_and_prunes_old_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            sub = projects / "proj" / "session" / "subagents"
            sub.mkdir(parents=True)
            now = time.time()
            (projects / "proj" / "session.jsonl").write_text(line("main", now, dollars(1)))
            (sub / "agent-1.jsonl").write_text(line("sub", now, dollars(2)))
            stale = projects / "proj" / "stale.jsonl"
            stale.write_text(line("stale", now, dollars(5)))
            old = now - (bg.RETENTION_DAYS + 1) * 86400
            os.utime(stale, (old, old))
            state = {"files": {"/gone.jsonl": {"offset": 5}}, "events": {"ancient": [old, 7.0, "m"]}}
            original = bg.PROJECTS_DIR
            bg.PROJECTS_DIR = projects
            try:
                events = bg.refresh(state)
            finally:
                bg.PROJECTS_DIR = original
            self.assertEqual(sorted(events), ["main", "sub"])
            self.assertNotIn("/gone.jsonl", state["files"])


class EvaluateTest(unittest.TestCase):
    # 2026-10-15 12:00 UTC. October has 31 days, so it ends at Nov 1 00:00 UTC.
    NOW = utc(2026, 10, 15, 12)
    CONFIG = {"monthly_usd": 1000, "daily_usd": None, "rolling_hours_usd": {"5": "pace"}, "pace_multiplier": 2.0}

    def setUp(self) -> None:
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self) -> None:
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def rows(self, events: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
             now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        merged = dict(bg.DEFAULT_CONFIG, **(config or self.CONFIG))
        return {r["name"]: r for r in bg.evaluate(merged, events, now or self.NOW)}

    def test_pace_formula(self) -> None:
        events = {"a": ev(utc(2026, 10, 2), 100.0)}
        window_start = self.NOW - 5 * 3600
        hours_left = (utc(2026, 11, 1) - window_start) / 3600
        self.assertEqual(hours_left, 401.0)
        self.assertAlmostEqual(self.rows(events)["5h"]["limit"], 2 * 5 * 900 / 401)

    def test_fresh_month_with_no_spend(self) -> None:
        # A constant rate that spends exactly $1000 over October is 1000/744 per hour.
        now = utc(2026, 10, 1, 5)
        self.assertAlmostEqual(self.rows({}, now=now)["5h"]["limit"], 2 * 5 * 1000 / 744)

    def test_spend_inside_the_window_does_not_move_its_limit(self) -> None:
        before = self.rows({"a": ev(utc(2026, 10, 2), 100.0)})["5h"]["limit"]
        after = self.rows({"a": ev(utc(2026, 10, 2), 100.0), "b": ev(self.NOW - 60, 50.0)})["5h"]
        self.assertAlmostEqual(after["limit"], before)
        self.assertAlmostEqual(after["spent"], 50.0)

    def test_underspending_loosens_and_overspending_tightens(self) -> None:
        light = self.rows({"a": ev(utc(2026, 10, 2), 50.0)})["5h"]["limit"]
        heavy = self.rows({"a": ev(utc(2026, 10, 2), 700.0)})["5h"]["limit"]
        self.assertGreater(light, heavy)

    def test_window_that_starts_last_month_is_anchored_at_the_month_start(self) -> None:
        now = utc(2026, 10, 1, 2)
        events = {"sept": ev(utc(2026, 9, 30, 23), 4.0), "sept_old": ev(utc(2026, 9, 10), 900.0)}
        row = self.rows(events, now=now)["5h"]
        self.assertAlmostEqual(row["limit"], 2 * 5 * 1000 / 744)  # September spend does not reduce October
        self.assertAlmostEqual(row["spent"], 4.0)  # but the window still counts the last 5 hours
        self.assertAlmostEqual(self.rows(events, now=now)["monthly"]["spent"], 0.0)

    def test_last_hours_of_the_month_do_not_blow_up(self) -> None:
        now = utc(2026, 10, 31, 23, 30)
        row = self.rows({"a": ev(utc(2026, 10, 2), 990.0)}, now=now)["5h"]
        # The window opened at 18:30, 5.5 hours before the month ends.
        self.assertAlmostEqual(row["limit"], 2 * 5 * 10 / 5.5)

    def test_month_already_spent_blocks(self) -> None:
        row = self.rows({"a": ev(utc(2026, 10, 2), 1000.0)})
        self.assertEqual(row["5h"]["limit"], 0.0)
        self.assertGreaterEqual(row["monthly"]["spent"], row["monthly"]["limit"])

    def test_monthly_counts_only_this_utc_month(self) -> None:
        events = {"sept": ev(utc(2026, 9, 30, 23, 59), 10.0), "oct": ev(utc(2026, 10, 1), 3.0)}
        self.assertAlmostEqual(self.rows(events)["monthly"]["spent"], 3.0)

    def test_pace_needs_a_monthly_budget(self) -> None:
        rows = self.rows({}, config={"monthly_usd": None, "rolling_hours_usd": {"5": "pace", "1": 3}})
        self.assertEqual(sorted(rows), ["1h"])
        self.assertEqual(rows["1h"]["limit"], 3.0)

    def test_daily_pace_and_fixed(self) -> None:
        events = {"a": ev(utc(2026, 10, 2), 100.0), "today": ev(utc(2026, 10, 15, 1), 7.0)}
        paced = self.rows(events, config=dict(self.CONFIG, daily_usd="pace"))["daily"]
        self.assertAlmostEqual(paced["limit"], 2 * 24 * 900 / (17 * 24))
        self.assertAlmostEqual(paced["spent"], 7.0)
        fixed = self.rows(events, config=dict(self.CONFIG, daily_usd=25))["daily"]
        self.assertEqual(fixed["limit"], 25.0)

    def test_describe_states_the_overage(self) -> None:
        over = {"name": "5h", "spent": 42.32, "limit": 30.96, "resets": "rolling"}
        self.assertEqual(bg.describe(over), "5h $42.32 of $30.96, $11.36 (37%) over (rolling)")
        under = {"name": "5h", "spent": 3.0, "limit": 30.0, "resets": "rolling"}
        self.assertNotIn("over", bg.describe(under))
        zero = {"name": "5h", "spent": 4.0, "limit": 0.0, "resets": "rolling"}
        self.assertIn("$4.00 over", bg.describe(zero))

    def test_projection_scales_the_remaining_budget_by_the_overage(self) -> None:
        events = {"early": ev(utc(2026, 10, 2), 100.0), "burst": ev(self.NOW - 3600, 40.0)}
        rows = bg.evaluate(dict(bg.DEFAULT_CONFIG, **self.CONFIG), events, self.NOW)
        window = next(r for r in rows if r["name"] == "5h")
        ratio = 40.0 / window["limit"]
        expected = 140.0 + ratio * (1000 - 140.0)
        text = bg.projection(self.CONFIG, rows, self.NOW)
        self.assertIn(f"October comes to about ${expected:,.0f} against your $1,000 budget", text)
        self.assertIn("last 5h's pace", text)

    def test_projection_at_exactly_the_cap_is_the_budget(self) -> None:
        rows = [{"name": "monthly", "spent": 200.0, "limit": 1000.0, "paced": False},
                {"name": "5h", "spent": 25.0, "limit": 25.0, "paced": True}]
        self.assertIn("about $1,000 against", bg.projection(self.CONFIG, rows, self.NOW))

    def test_projection_uses_the_most_intense_paced_window(self) -> None:
        rows = [{"name": "monthly", "spent": 0.0, "limit": 1000.0, "paced": False},
                {"name": "daily", "spent": 10.0, "limit": 100.0, "paced": True},
                {"name": "5h", "spent": 60.0, "limit": 30.0, "paced": True}]
        self.assertIn("last 5h's pace", bg.projection(self.CONFIG, rows, self.NOW))
        self.assertIn("about $2,000", bg.projection(self.CONFIG, rows, self.NOW))

    def test_no_projection_for_an_idle_window(self) -> None:
        rows = [{"name": "monthly", "spent": 175.0, "limit": 1000.0, "paced": False},
                {"name": "5h", "spent": 0.0, "limit": 30.0, "paced": True}]
        self.assertIsNone(bg.projection(self.CONFIG, rows, self.NOW))

    def test_no_projection_without_a_paced_budget(self) -> None:
        monthly = {"name": "monthly", "spent": 10.0, "limit": 1000.0, "paced": False}
        fixed = {"name": "5h", "spent": 50.0, "limit": 10.0, "paced": False}
        paced = {"name": "5h", "spent": 50.0, "limit": 10.0, "paced": True}
        self.assertIsNone(bg.projection(self.CONFIG, [monthly, fixed], self.NOW))
        self.assertIsNone(bg.projection(self.CONFIG, [paced], self.NOW))
        spent_out = dict(monthly, spent=1000.0)
        self.assertIsNone(bg.projection(self.CONFIG, [spent_out, dict(paced, limit=0.0)], self.NOW))

    def test_frees_at_names_when_the_window_drops_under(self) -> None:
        t0 = self.NOW - 4 * 3600
        events = {"a": ev(t0, 6.0), "b": ev(t0 + 3600, 6.0), "c": ev(t0 + 7200, 1.0)}
        free = bg.frees_at(events, self.NOW - 5 * 3600, self.NOW + 1, 10.0, 5 * 3600)
        self.assertEqual(free, t0 + 5 * 3600)  # dropping "a" leaves $7, under $10
        self.assertIsNone(bg.frees_at(events, self.NOW - 5 * 3600, self.NOW + 1, 20.0, 5 * 3600))


class HookTest(unittest.TestCase):
    """Runs the script as Claude Code does: a subprocess with hook JSON on stdin."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "guard"
        self.home.mkdir()
        self.projects = root / "claude" / "projects" / "proj"
        self.projects.mkdir(parents=True)
        self.claude_dir = root / "claude"
        # A run inside a Claude Code hook would otherwise leak real plugin options into the tests.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_OPTION_")}
        self.env.update(BUDGET_GUARD_HOME=str(self.home), CLAUDE_CONFIG_DIR=str(self.claude_dir))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def spend(self, amount: float, ago_s: float = 600, msg_id: Optional[str] = None) -> None:
        with (self.projects / "s.jsonl").open("a") as f:
            f.write(line(msg_id or f"m{time.time_ns()}", time.time() - ago_s, dollars(amount)))

    def config(self, **values: Any) -> None:
        (self.home / "config.json").write_text(json.dumps(values))

    def run_script(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, env=self.env,
                              capture_output=True, text=True, timeout=30)

    def hook(self, event: str, prompt: str = "do some work") -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {"hook_event_name": event, "session_id": "s"}
        if event == "UserPromptSubmit":
            payload["prompt"] = prompt
        result = self.run_script("hook", stdin=json.dumps(payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def override_until(self) -> float:
        path = self.home / "override.json"
        return json.loads(path.read_text())["until"] if path.exists() else 0

    def test_override_survives_concurrent_hooks_from_busy_sessions(self) -> None:
        # Other sessions rewrite state.json on every tool call; none may erase an override.
        self.lock_out()
        self.run_script("status")  # warm the cache so concurrent runs finish fast
        for _ in range(5):
            busy = [subprocess.Popen([sys.executable, str(SCRIPT), "hook"], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, env=self.env, text=True) for _ in range(4)]
            for p in busy:
                p.stdin.write(json.dumps({"hook_event_name": "PreToolUse"}))
                p.stdin.close()
            self.hook("UserPromptSubmit", "budget override 60")
            for p in busy:
                p.wait(timeout=30)
            self.assertGreater(self.override_until(), time.time())
            self.run_script("allow", "0")

    def lock_out(self) -> None:
        self.config(rolling_hours_usd={"5": 1})
        self.spend(5)

    def test_lockout_messages_explain_both_overrides(self) -> None:
        self.lock_out()
        terminal = f"{SCRIPT} allow 30"
        reason = self.hook("UserPromptSubmit")["reason"]
        self.assertIn("send this as a message: budget override 30", reason)
        self.assertIn(terminal, reason)
        deny = self.hook("PreToolUse")
        self.assertIn("budget override 30", deny["systemMessage"])
        self.assertIn(terminal, deny["systemMessage"])
        self.assertIn("budget override 30", deny["hookSpecificOutput"]["permissionDecisionReason"])

    def test_override_phrase_unlocks_without_reaching_the_model(self) -> None:
        self.lock_out()
        out = self.hook("UserPromptSubmit", "budget override 30")
        self.assertEqual(out["decision"], "block")  # the phrase itself is never sent to the model
        self.assertIn("limits suspended until", out["reason"])
        self.assertAlmostEqual(self.override_until(), time.time() + 1800, delta=60)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.assertIsNone(self.hook("PreToolUse"))

    def test_override_phrase_minutes_case_and_default(self) -> None:
        self.lock_out()
        self.hook("UserPromptSubmit", "budget override 5")
        self.assertAlmostEqual(self.override_until(), time.time() + 300, delta=60)
        self.hook("UserPromptSubmit", "  Budget   OVERRIDE \n")
        self.assertAlmostEqual(self.override_until(), time.time() + 1800, delta=60)

    def test_override_phrase_zero_cancels_an_active_override(self) -> None:
        self.lock_out()
        self.hook("UserPromptSubmit", "budget override 30")
        out = self.hook("UserPromptSubmit", "budget override 0")
        self.assertIn("override cancelled", out["reason"])
        self.assertEqual(self.override_until(), 0)
        self.assertIn("limit reached", self.hook("UserPromptSubmit")["reason"])

    def test_prompts_that_only_mention_the_phrase_are_ordinary_prompts(self) -> None:
        self.lock_out()
        for prompt in ("budget override please", "what does budget override 30 do?", "budget override -5"):
            out = self.hook("UserPromptSubmit", prompt)
            self.assertIn("limit reached", out["reason"], prompt)
        self.assertEqual(self.override_until(), 0)

    def test_override_phrase_works_when_not_locked_out(self) -> None:
        self.config(rolling_hours_usd={"5": 100})
        self.spend(1)
        self.assertIn("limits suspended", self.hook("UserPromptSubmit", "budget override 10")["reason"])

    def test_under_limit_is_silent(self) -> None:
        self.config(rolling_hours_usd={"5": 10})
        self.spend(2)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.assertIsNone(self.hook("PreToolUse"))

    def test_warns_past_warn_at(self) -> None:
        self.config(rolling_hours_usd={"5": 10}, warn_at=0.8)
        self.spend(8.5)
        out = self.hook("UserPromptSubmit")
        self.assertIn("5h $8.50 of $10.00", out["systemMessage"])
        self.assertNotIn("decision", out)
        self.assertIsNone(self.hook("PreToolUse"))

    def test_blocks_prompt_and_tools_when_over(self) -> None:
        self.config(rolling_hours_usd={"5": 10})
        self.spend(6, ago_s=3 * 3600)
        self.spend(6, ago_s=600)
        out = self.hook("UserPromptSubmit")
        self.assertEqual(out["decision"], "block")
        self.assertIn("5h $12.00 of $10.00", out["reason"])
        self.assertIn("allow 30", out["reason"])
        deny = self.hook("PreToolUse")["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertEqual(deny["hookEventName"], "PreToolUse")
        self.assertIn("5h $12.00 of $10.00", deny["permissionDecisionReason"])
        self.assertIn("allow 30", deny["permissionDecisionReason"])

    def test_spend_older_than_the_window_does_not_count(self) -> None:
        self.config(rolling_hours_usd={"5": 10})
        self.spend(50, ago_s=6 * 3600)
        self.spend(1)
        self.assertIsNone(self.hook("UserPromptSubmit"))

    def test_monthly_pace_end_to_end(self) -> None:
        self.config(monthly_usd=1000, rolling_hours_usd={"5": "pace"})
        self.spend(1)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.spend(1000, ago_s=60)  # blows both the monthly cap and the 5h pace
        out = self.hook("UserPromptSubmit")
        self.assertEqual(out["decision"], "block")
        self.assertIn("monthly", out["reason"])
        self.assertIn("5h", out["reason"])

    def test_messages_carry_the_overage_and_projection(self) -> None:
        self.config(monthly_usd=1000, rolling_hours_usd={"5": "pace"})
        self.spend(200, ago_s=60)  # far over any 5h pace, well under the monthly cap
        reason = self.hook("UserPromptSubmit")["reason"]
        self.assertRegex(reason, r"5h \$200\.00 of \$[\d.]+, \$[\d.]+ \(\d+%\) over")
        self.assertRegex(reason, r"comes to about \$[\d,]+ against your \$1,000 budget")
        deny = self.hook("PreToolUse")
        self.assertIn("comes to about $", deny["systemMessage"])
        self.assertIn("comes to about $", deny["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIn("comes to about $", self.run_script("status").stdout)

    def test_warning_carries_the_projection(self) -> None:
        self.config(monthly_usd=1000, rolling_hours_usd={"5": "pace"}, warn_at=0.0001)
        self.spend(0.5)
        message = self.hook("UserPromptSubmit")["systemMessage"]
        self.assertNotIn(" over", message)
        self.assertIn("comes to about $", message)

    def test_plugin_options_from_the_environment_set_the_limits(self) -> None:
        self.config(monthly_usd=50, rolling_hours_usd={"5": "pace"}, pace_multiplier=1)
        self.spend(100)  # over a $50 month
        self.env.update(CLAUDE_PLUGIN_OPTION_MONTHLY_BUDGET_USD="5000", CLAUDE_PLUGIN_OPTION_PACE_MULTIPLIER="24")
        self.assertIsNone(self.hook("UserPromptSubmit"))  # the options win over config.json
        status = self.run_script("status").stdout
        self.assertIn("monthly $100.00 of $5000.00", status)
        self.assertIn("Limits: $5,000 a month, with a 5h cap at 24x pace.", status)

    def test_terminal_commands_read_the_saved_plugin_options(self) -> None:
        self.claude_dir.mkdir(exist_ok=True)
        (self.claude_dir / "settings.json").write_text(json.dumps({"pluginConfigs": {
            "other-plugin@x": {"options": {"monthly_budget_usd": 1}},
            "budget-guard@budget-guard": {"options": {"monthly_budget_usd": 1200, "pace_multiplier": 3}},
        }}))
        status = self.run_script("status").stdout
        self.assertIn("monthly $0.00 of $1200.00", status)
        self.assertIn("Limits: $1,200 a month, with a 5h cap at 3x pace.", status)
        self.assertIn("/config", status)

    def test_defaults_come_from_the_plugin_manifest(self) -> None:
        # Claude Code exports only values the user saved, so the defaults must come from plugin.json.
        manifest = json.loads((SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
        status = self.run_script("status").stdout
        monthly, multiplier = manifest["monthly_budget_usd"]["default"], manifest["pace_multiplier"]["default"]
        self.assertIn(f"Limits: ${monthly:,.0f} a month, with a 5h cap at {multiplier:g}x pace.", status)
        self.assertNotIn("comes to about", status)  # no spend in the window, so no projection

    def test_status_outside_a_plugin_without_a_budget_says_so(self) -> None:
        loose = Path(self.tmp.name) / "loose_budget_guard.py"
        loose.write_text(SCRIPT.read_text())
        result = subprocess.run([sys.executable, str(loose), "status"], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.assertIn("No monthly budget is set.", result.stdout)

    def test_status_phrase_answers_without_reaching_the_model(self) -> None:
        self.config(monthly_usd=1000, rolling_hours_usd={"5": "pace"})
        self.spend(2)
        for prompt in ("budget status", "  Budget STATUS \n"):
            out = self.hook("UserPromptSubmit", prompt)
            self.assertEqual(out["decision"], "block")
            self.assertIn("monthly $2.00 of $1000.00", out["reason"])
            self.assertIn("/config", out["reason"])
        self.assertIsNone(self.hook("UserPromptSubmit", "budget status please"))  # an ordinary prompt

    def test_status_phrase_works_while_locked_out(self) -> None:
        self.lock_out()
        reason = self.hook("UserPromptSubmit", "budget status")["reason"]
        self.assertTrue(reason.startswith("Budget guard status:"))
        self.assertIn("over", reason)

    def test_tool_blocking_can_be_turned_off(self) -> None:
        self.config(rolling_hours_usd={"5": 1}, block_tools_when_over=False)
        self.spend(5)
        self.assertIsNone(self.hook("PreToolUse"))
        self.assertEqual(self.hook("UserPromptSubmit")["decision"], "block")

    def test_override_suspends_and_cancels(self) -> None:
        self.config(rolling_hours_usd={"5": 1})
        self.spend(5)
        self.assertEqual(self.run_script("allow", "30").returncode, 0)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.assertIsNone(self.hook("PreToolUse"))
        self.run_script("allow", "0")
        self.assertEqual(self.hook("UserPromptSubmit")["decision"], "block")

    def test_new_spend_is_picked_up_between_calls(self) -> None:
        self.config(rolling_hours_usd={"5": 10})
        self.spend(5)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.spend(6)
        self.assertEqual(self.hook("UserPromptSubmit")["decision"], "block")

    def test_broken_config_fails_open_and_logs(self) -> None:
        (self.home / "config.json").write_text("{not json")
        self.spend(500)
        self.assertIsNone(self.hook("UserPromptSubmit"))
        self.assertIn("Traceback", (self.home / "errors.log").read_text())

    def test_corrupt_state_is_rebuilt(self) -> None:
        self.config(rolling_hours_usd={"5": 10})
        self.spend(11)
        (self.home / "state.json").write_text("garbage")
        self.assertEqual(self.hook("UserPromptSubmit")["decision"], "block")

    def test_status_and_report(self) -> None:
        self.config(monthly_usd=1000)
        self.spend(3)
        status = self.run_script("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("monthly $3.00 of $1000.00", status.stdout)
        self.assertIn("5h $3.00 of $", status.stdout)
        report = self.run_script("report", "1")
        self.assertIn("claude-haiku-4-5 $3.00", report.stdout)


if __name__ == "__main__":
    unittest.main()
