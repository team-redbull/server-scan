"""PreToolUse on Bash: refuse a `git commit` carrying an agent-attribution trailer.

CLAUDE.md convention 2: the user is the only visible contributor. A
`Claude-Session:` URL reached a commit and PR #9 once despite the rule
being written down, because the harness re-asks for one every session.
Exit 2 blocks the command and shows the reason to Claude.
"""

import json
import re
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

command = str(payload.get("tool_input", {}).get("command", ""))
if not re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
    sys.exit(0)

forbidden = re.compile(r"Co-Authored-By|Claude-Session|Generated with|\U0001f916", re.IGNORECASE)
match = forbidden.search(command)
if match:
    print(
        f"Blocked: the commit message contains an agent-attribution trailer "
        f"({match.group(0)!r}). CLAUDE.md convention 2: no Co-Authored-By, no "
        f"Claude-Session URL, no generated-with footer — the user is the only "
        f"visible contributor. Remove it and commit again.",
        file=sys.stderr,
    )
    sys.exit(2)
