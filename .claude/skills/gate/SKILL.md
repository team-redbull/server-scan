---
name: gate
description: Run the full local CI gate (ruff, ty, comment density, import-linter, helm lint/template, frontend lint/typecheck/vitest/build) before calling work done. CLAUDE.md convention 7.
disable-model-invocation: true
---

Run the whole gate, or a subset, and report every failing step — do not
stop at the first one:

```bash
bash .claude/skills/gate/gate.sh $ARGUMENTS
```

Flags: `--backend`, `--helm`, `--frontend` (any combination; none = all).
`pytest` is deliberately not here — it needs the dev stack up
(`scripts/dev-up.sh up`) and takes minutes; run `uv run pytest -q`
separately when the change touches behaviour.

If `ruff format --check` fails, run `uv run ruff format .` and re-run —
never hand-fix formatting. If comment density fails, fix the file it
names; never touch `scripts/comment-density-baseline.txt` by hand.

After a green gate, do the step no command covers: `/docs-sweep`.
