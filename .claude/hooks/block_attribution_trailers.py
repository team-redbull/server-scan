"""PreToolUse on Bash: refuse a commit or PR text carrying an agent-attribution trailer.

CLAUDE.md convention 2: the user is the only visible contributor. A
`Claude-Session:` URL reached a commit and PR #9 once despite the rule
being written down, because the harness re-asks for one every session.
Covers `git commit` and `gh pr create|edit`, including a message read from a
file (`-F`, `--file`, `--body-file`). Exit 2 blocks the command and shows the
reason to Claude.
"""

import contextlib
import json
import re
import sys
from pathlib import Path

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

command = str(payload.get("tool_input", {}).get("command", ""))
is_commit = re.search(r"\bgit\b[^|;&]*\bcommit\b", command)
is_pr = re.search(r"\bgh\s+pr\s+(create|edit)\b", command)
if not (is_commit or is_pr):
    sys.exit(0)

text = command
for name in re.findall(r"(?:\s-F|\s--file|\s--body-file)[=\s]+['\"]?([^\s'\"]+)", command):
    with contextlib.suppress(OSError):
        text += "\n" + Path(name).read_text(errors="ignore")

forbidden = re.compile(r"Co-Authored-By|Claude-Session|Generated with|\U0001f916", re.IGNORECASE)
match = forbidden.search(text)
if match:
    print(
        f"Blocked: the commit message or PR text contains an agent-attribution trailer "
        f"({match.group(0)!r}). CLAUDE.md convention 2: no Co-Authored-By, no "
        f"Claude-Session URL, no generated-with footer — the user is the only "
        f"visible contributor. Remove it and try again.",
        file=sys.stderr,
    )
    sys.exit(2)
