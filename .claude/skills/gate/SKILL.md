---
name: gate
description: Run the full local CI gate (ruff, ty, comment density, import-linter, pip-audit, deptry, npm audit, helm lint/template, frontend lint/typecheck/vitest/build) before calling work done or pushing. CLAUDE.md convention 7.
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

The audit steps (`pip-audit`, `deptry`, `npm audit`) are CI's own and need the
network. They fail on an advisory published since the last green run with NO
change in your code — that is how a push that passed every local lint step
still went red on 2026-09-30 (urllib3 2.7.0, undici). Fix with
`uv lock --upgrade-package <pkg>` (then re-export `requirements.txt` /
`pylock.toml`, docs/air-gap.md) or `npm audit fix`. An audit that failed
because the network was down is not a pass.

**The gate plus `pytest` and, before a push, `gh run watch` on the pushed
commit is the whole picture** — the gate is not "CI"; E2E and pytest run only
there or separately. After every push to `main`, watch CI to the end and
report the result; a push publishes a release only if CI is green.

The `deploy render` step clones team-redbull/redbull-platform and renders our
templates against ITS values.yaml, like CI's Deploy job. A failure there means a
template reads a `.Values` key the platform values do not have: give it a
`default` (and add the key to the platform values when it should be set).

If `ruff format --check` fails, run `uv run ruff format .` and re-run —
never hand-fix formatting. If comment density fails, fix the file it
names; never touch `scripts/comment-density-baseline.txt` by hand.

After a green gate, do the step no command covers: `/docs-sweep`.
