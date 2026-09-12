"""PreToolUse on Edit/Write: the comment-density baseline is never edited by hand.

CLAUDE.md convention 8: `scripts/comment-density-baseline.txt` may only
shrink, and only through `check_comment_density.py --regenerate`. Adding
a line to make a new violation pass is the one thing it exists to
prevent.
"""

import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

path = str(payload.get("tool_input", {}).get("file_path", ""))
if path.endswith("comment-density-baseline.txt"):
    print(
        "Blocked: scripts/comment-density-baseline.txt is regenerated, never "
        "edited. Fix the violation in the file it names, then run "
        "`uv run python scripts/check_comment_density.py --regenerate`.",
        file=sys.stderr,
    )
    sys.exit(2)
