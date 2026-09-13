---
name: docs-sweep
description: After finishing a change, find every doc it made wrong and fix it in the same commit — README, docs/architecture.md, docs/arc42.md (§9 ADR index), deploy/README.md, CLAUDE.md, .env.example. CLAUDE.md convention 11.
---

A change is not finished until the docs it made wrong are fixed, in the
same commit. Walk this list against `git diff` (staged + unstaged) and
the change you just made; for each file, decide "does anything here now
describe the code wrongly?" and fix it. Report what you checked and what
you changed — "nothing to change" is a valid answer per file, silence is
not.

| File | Look at |
|---|---|
| `README.md` | status list, data-flow diagram, project layout, any seeded figures |
| `docs/architecture.md` | the subsystem section for what was touched |
| `docs/arc42.md` | **§9 ADR index — a new ADR needs a row or nothing links to it**; §5 deployable units, §7 deployment, §8 quality table, §11 risks, §12 glossary |
| `deploy/README.md` | charts, values, CronJobs — including its opening sentence |
| `CLAUDE.md` | "Key technical facts" for a *cross-cutting* trap; "Where to continue right now" holds only the newest unit of work — move the previous one to `docs/notes/session-log.md` |
| `.claude/rules/*.md` | a collector / storage-query / frontend trap goes in `collectors.md`, `mongodb.md` or `frontend.md`, not CLAUDE.md |
| `.env.example` | any new or renamed variable |

Also: a new decision needs an ADR under `docs/adr/` with the code
carrying a one-line pointer, not the reasoning (convention 8). A false
statement noticed on the way counts as part of the job — fix it and say
so in the commit body.

Quick checks that catch the usual misses:

```bash
git diff --name-only HEAD | grep -E '^docs/adr/' && grep -c 'adr/' docs/arc42.md
git diff HEAD -- backend/app/config/settings.py | grep -E '^\+.*: ' && grep -c INVENTORY_ .env.example
```
