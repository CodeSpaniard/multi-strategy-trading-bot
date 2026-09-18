#!/bin/bash
# Forced command for the cockpit SSH key.
#
# This file IS the security boundary. The cockpit key is pinned to this script in
# authorized_keys, so whatever the client asks for arrives only as a string in
# SSH_ORIGINAL_COMMAND — it is never executed. Anything not in the allowlist below
# is refused. That means the cockpit cannot run `systemctl stop`, cannot read
# .env.live, and cannot open a shell, regardless of bugs in the cockpit code.
#
# Adding a case here widens what the cockpit can do. Only ever add read-only
# commands, and never anything that takes free-form arguments.
set -euo pipefail

REPO=/home/bot/trading-bot
PY="$REPO/.venv/bin/python"
cd "$REPO"

# Every pattern below is quoted. These are an allowlist of literal strings, not
# globs — quoting keeps a future entry containing * ? or [ from silently
# widening what the cockpit can run.
case "${SSH_ORIGINAL_COMMAND:-status}" in
    ""|"status")
        exec "$PY" tools/status_json.py
        ;;
    "status --broker")
        exec "$PY" tools/status_json.py --broker
        ;;
    *)
        # Deliberately does NOT echo the requested command back. Reflecting
        # attacker-controlled input buys nothing for enforcement, and it kept a
        # shell->python boundary in the one code path that only untrusted input
        # ever reaches. A fixed string has nothing to audit.
        printf '{"error":"refused","detail":"command not in cockpit allowlist"}\n' >&2
        exit 42
        ;;
esac
