#!/bin/sh
# Installs or updates Budget Guard, and optionally sets your monthly budget.
#
#   curl -fsSL https://raw.githubusercontent.com/mpasternak-lv/claude-budget-guard/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/mpasternak-lv/claude-budget-guard/main/install.sh | sh -s -- 1700
#   curl -fsSL https://raw.githubusercontent.com/mpasternak-lv/claude-budget-guard/main/install.sh | sh -s -- --uninstall
#
# Safe to run again: an existing install is updated rather than reinstalled.
# BUDGET_GUARD_REPO installs from another copy of the repository (owner/name).
set -eu

REPO="${BUDGET_GUARD_REPO:-mpasternak-lv/claude-budget-guard}"
MARKETPLACE="budget-guard"
PLUGIN="budget-guard@budget-guard"
BUDGET="${1:-}"

# The VS Code extension does not put claude on PATH, so fall back to the copy
# each editor extension bundles, newest version first.
find_claude() {
  if command -v claude >/dev/null 2>&1; then
    command -v claude
    return
  fi
  for editor in .vscode .vscode-insiders .cursor .windsurf; do
    ls -d "$HOME/$editor"/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null
  done | sort -V | tail -n 1
}

CLAUDE="$(find_claude)"
if [ -z "$CLAUDE" ] || [ ! -x "$CLAUDE" ]; then
  echo "Could not find Claude Code. Install the CLI or the VS Code extension first." >&2
  exit 1
fi
if [ ! -x /usr/bin/python3 ]; then
  echo "/usr/bin/python3 is missing. On a Mac, install it with: xcode-select --install" >&2
  exit 1
fi
echo "Using $CLAUDE"

if [ "$BUDGET" = "--uninstall" ]; then
  "$CLAUDE" plugin uninstall "$PLUGIN" || true
  "$CLAUDE" plugin marketplace remove "$MARKETPLACE" || true
  echo "Removed. Its saved state is in ~/.claude/budget-guard, which you can delete."
  exit 0
fi

# Prints the repository the budget-guard marketplace currently points at, if any.
current_repo="$("$CLAUDE" plugin marketplace list --json 2>/dev/null | /usr/bin/python3 -c '
import json, sys
try:
    entries = json.load(sys.stdin)
except ValueError:
    entries = []
print(next((e.get("repo", "") for e in entries if e.get("name") == "budget-guard"), ""))
')"

# Runs a claude command, dropping its "/plugin configure" hint: the VS Code
# extension has no /plugin command, and `budget set` replaces it.
quiet() {
  output="$("$@" 2>&1)" || { printf '%s\n' "$output" >&2; return 1; }
  printf '%s\n' "$output" | grep -v "userConfig options not yet set" || true
}

# The saved settings as a JSON object of strings, the shape --values-stdin takes.
saved_settings="$(/usr/bin/python3 -c '
import json, os, sys
path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")), "settings.json")
try:
    configs = json.load(open(path)).get("pluginConfigs") or {}
except (OSError, ValueError, AttributeError):
    configs = {}
options = (configs.get("budget-guard@budget-guard") or {}).get("options") or {}
print(json.dumps({k: str(v) for k, v in options.items()}) if options else "")
')"

if [ "$current_repo" = "$REPO" ]; then
  quiet "$CLAUDE" plugin marketplace update "$MARKETPLACE"
else
  if [ -n "$current_repo" ]; then
    echo "Switching Budget Guard from $current_repo to $REPO"
    # Removing a marketplace deletes its plugins' saved settings; they are put back below.
    quiet "$CLAUDE" plugin marketplace remove "$MARKETPLACE"
  fi
  quiet "$CLAUDE" plugin marketplace add "$REPO"
fi

if "$CLAUDE" plugin list --json 2>/dev/null | grep -q "\"$PLUGIN\""; then
  quiet "$CLAUDE" plugin update "$PLUGIN"
else
  quiet "$CLAUDE" plugin install "$PLUGIN"
  if [ -n "$saved_settings" ]; then
    printf '%s' "$saved_settings" | quiet "$CLAUDE" plugin configure "$PLUGIN" --values-stdin
    echo "Kept your saved settings."
  fi
fi

SCRIPT="$("$CLAUDE" plugin list --json | /usr/bin/python3 -c '
import json, sys
print(next(p["installPath"] for p in json.load(sys.stdin) if p["id"] == "budget-guard@budget-guard"))
')/scripts/budget_guard.py"

if [ -n "$BUDGET" ]; then
  /usr/bin/python3 "$SCRIPT" set "$BUDGET"
fi

echo
/usr/bin/python3 "$SCRIPT" status
echo
echo "Done. Start a new Claude Code session to load it, then send: budget status"
