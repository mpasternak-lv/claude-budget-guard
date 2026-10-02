# Budget Guard for Claude Code

Short-term spend limits for Claude Code on usage-based billing.

Anthropic's spend limits are monthly, so nothing stops one heavy afternoon from eating a week of budget. Budget Guard adds a 5-hour cap that is paced to your monthly budget. When you go over it, Claude Code stops, tells you how far over you are and what the month will cost at that pace, and shows you how to keep going if you need to.

It works the same in the VS Code extension and the terminal CLI.

## Install

Paste this into a terminal, either Terminal.app or the terminal inside VS Code. It works whether you use the VS Code extension or the CLI. The number at the end is your monthly budget in dollars:

```
curl -fsSL https://raw.githubusercontent.com/mpasternak-lv/claude-budget-guard/main/install.sh | sh -s -- 1700
```

Leave off `-s -- 1700` to start with the $300 default. Then start a new Claude Code session and send `budget status` to check it is running.

The script finds Claude Code on its own, including the copy bundled with the VS Code extension, so you don't need `claude` on your PATH. It needs `/usr/bin/python3`, which every Mac with the Xcode command line tools has.

If you use the terminal CLI, you can also install from inside Claude Code. The VS Code extension doesn't have the `/plugin` command, so this only works in the CLI:

```
/plugin marketplace add mpasternak-lv/claude-budget-guard
/plugin install budget-guard@budget-guard
```

## Set and change your limits

Send one of these as a message in Claude Code. Budget Guard saves it and uses it from your next message on, in every open session.

```
budget set 1700
budget set multiplier 4
```

| Setting | Default | What it does |
| :- | :- | :- |
| Monthly budget (USD) | 300 | Your Claude Code budget for the calendar month. Set it to the monthly limit your admin gave you. |
| 5-hour cap multiplier | 5 | How much faster than an even, round-the-clock pace a 5-hour window may spend. 5 suits normal working hours. Raise it for a looser cap. |

`budget set` saves to the same place as the CLI's `/plugin configure budget-guard@budget-guard`, so either way works in the CLI.

## What happens as you work

- **Below 80% of the 5-hour cap**, nothing happens.
- **Past 80%**, each new message shows a warning with your spend and a month-end projection.
- **Over the cap**, new messages are refused. If Claude is in the middle of a task, it stops at its next tool call and tells you what is done and what remains. The message says how far over you are, what the month will cost at this pace, and when you are back under the cap.
- **Over the monthly budget**, the same, until the month resets at 00:00 UTC on the 1st. That is the same boundary Anthropic's own monthly limit uses.

## Commands

Type these as a message. Budget Guard answers them itself, so they never reach the model and cost nothing, even while you are locked out.

| Message | What it does |
| :- | :- |
| `budget status` | Shows your spend against each limit, the month-end projection, and your settings |
| `budget set 1700` | Sets your monthly budget |
| `budget set multiplier 4` | Sets the 5-hour cap multiplier |
| `budget override 30` | Suspends the limits for 30 minutes, or any number of minutes you give |
| `budget override 0` | Ends an override early |

An override covers every Claude Code session on your machine.

## How the 5-hour cap is calculated

```
cap = multiplier x 5 hours x (budget left this month / hours left in the month)
```

Both values are taken at the start of the 5-hour window. At the start of a 31-day month, with the default $300 budget and a multiplier of 5, the cap is 5 x 5 x 300 / 744, about $10.08. With a $1,700 budget it is about $57.12. Spending less than that early in the month raises the cap later, and spending more lowers it, so you are always paced toward the budget you set.

The month-end projection uses the same idea. Spending right at the cap lands the month on your budget, so being 40% over the cap projects 40% over on what is left of your budget.

## What it can and can't see

- **The numbers are estimates.** They come from Claude Code's own session logs on your machine (`~/.claude/projects`), priced at Anthropic's published API rates. They are not your invoice, though in testing they matched an independent cost tool to the cent.
- **Only this machine.** It counts every Claude Code session on your computer together. It can't see claude.ai chat or Claude Code on another computer.
- **It's a guardrail you control.** You can turn it off at any time, so your admin's monthly limit stays the hard backstop.
- **It fails open.** If Budget Guard itself breaks, it logs the error to `~/.claude/budget-guard/errors.log` and lets you keep working.
- **Model prices are built in.** A model it doesn't recognize yet is priced at the most expensive tier, so it never counts as free.

## Updates and removal

To update, run the install command again. It updates an existing install instead of reinstalling it, and keeps your settings. Then start a new session.

To remove it:

```
curl -fsSL https://raw.githubusercontent.com/mpasternak-lv/claude-budget-guard/main/install.sh | sh -s -- --uninstall
```

## Mirror

A mirror of this repository is kept at [MikeyPWhatAG/claude-budget-guard](https://github.com/MikeyPWhatAG/claude-budget-guard). To install from the mirror instead, use:

```
curl -fsSL https://raw.githubusercontent.com/MikeyPWhatAG/claude-budget-guard/main/install.sh | BUDGET_GUARD_REPO=MikeyPWhatAG/claude-budget-guard sh
```

## Development

The guard is one standard-library Python script, `plugins/budget-guard/scripts/budget_guard.py`. Its tests need no dependencies:

```
python3 -m unittest discover -s tests -v
claude plugin validate .
```

`tests/live_e2e.py` checks the installed plugin end to end through real headless Claude Code sessions: lockouts, overrides, a mid-turn stop, `budget set`, warnings, hook latency, concurrent sessions, and failing open. It spends a few cents on Haiku. While it runs, it suspends the limits in your other sessions and changes your saved budget, then restores both. Run it after installing a new version:

```
python3 tests/live_e2e.py
```

Bump `version` in `plugins/budget-guard/.claude-plugin/plugin.json` with every change. Installed copies stay on the version they have until it changes.
