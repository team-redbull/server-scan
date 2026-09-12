"""PostToolUse: ruff-format a Python file Claude just edited.

CLAUDE.md convention 7 — a commit has shipped that failed CI on
`ruff format --check` alone. Formatting on every edit removes that
class. Silent on non-Python files and on any failure: a hook must never
block the edit it follows.
"""

import json
import subprocess
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)

path = str(payload.get("tool_input", {}).get("file_path", ""))
if not path.endswith(".py"):
    sys.exit(0)

subprocess.run(  # noqa: S603 — fixed argv, only the edited path varies
    ["uv", "run", "--quiet", "ruff", "format", "--quiet", path],  # noqa: S607
    check=False,
    capture_output=True,
    timeout=20,
)
