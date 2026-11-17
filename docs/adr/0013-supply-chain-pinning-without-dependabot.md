# ADR-0013: CI actions are pinned to commit SHAs, and kept current by hand

## Status

Accepted

## Context

Every action in `.github/workflows/ci.yml` was referenced by a moving tag
(`actions/checkout@v4`, `docker/login-action@v4`, ...). Three of them were
also three majors behind: checkout, setup-node and upload-artifact were
all on v4 while v7 was current.

The version lag is the smaller problem. A tag is a mutable pointer:
whoever controls an action's repository can re-point `v7` at new code, and
every workflow that says `@v7` runs it on the next push with no diff
anywhere for a reviewer to see. That is not theoretical — it is how
`tj-actions/changed-files` was used in 2025 to dump CI secrets out of
thousands of repositories. This repository's `publish` job holds
`contents: write` and a GHCR token, so a compromised action there can
push tags and images under the project's name.

## Decision

**Every action is pinned to a full 40-character commit SHA**, with the
human-readable version in a trailing comment:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

A SHA cannot be re-pointed. The comment is what makes the pin legible;
without it, nobody can tell at a glance whether a pin is current.

**Before pinning, verify the tag resolves.** This repository has twice
been broken by assuming a rolling major tag exists: `github-tag-action`
publishes no `v6`, and `setup-uv` publishes no `v8`/`v9`/`v10` (all 404).
Resolve `refs/tags/<tag>` through the API and dereference annotated tags
to their commit — do not hand-copy from a README.

**The pins are maintained by hand.** Dependabot was configured for this
and then removed; see below.

## Consequences

- **A pinned action never updates itself.** Without a periodic pass these
  rot into "old and unpatched", which is the opposite of the posture the
  pinning exists for. This is a standing obligation, not a one-off: see
  the checklist in `CLAUDE.md`.
- **Version bumps are reviewable.** Upgrading is now a visible diff of a
  SHA and a comment, rather than something that silently changed under a
  tag.

### Dependabot was tried and removed

`.github/dependabot.yml` was added to automate exactly the maintenance
this decision creates. It opened seven PRs within a minute, and two of
them were wrong in a way the tool could not know about.

Dependabot's `uv` ecosystem also recognises pip-style requirements files,
so it edited `requirements.txt` — which in this repository is a
*generated export* for air-gapped mirroring, not a source manifest — as
though it were the place dependencies are declared. Those PRs changed
`requirements.txt` alone, leaving it asserting versions `uv.lock` does
not contain, with nothing in CI checking the two agree. One of them bumped
`pydantic-core`, a transitive dependency that `pydantic` pins exactly.

It was removed rather than narrowed, on the user's call. The tradeoff is
explicit: no automated bumps, and the maintenance obligation above is
real. If it is ever reinstated, the `uv` ecosystem must be scoped to
`pyproject.toml`/`uv.lock` only, and CI should gain a check that the
exports still match the lock.

Worth recording what the experiment did establish, since the PRs are
gone: `mypy` 2.3.0 is clean on this codebase (verified — 110 files, 484
tests), and `@types/node` 26 type-checks but describes a Node version
newer than the 24 CI runs, so it should wait for the runtime to move.

### Version bumps, not just upgrades

A separate pass found what version-bump automation does not look for:
`pip-audit` reported seven known vulnerabilities in `python-multipart`
0.0.9, the only package in the tree with any. It was a *direct*
dependency, and Dependabot had not flagged it — automation of this kind
answers "is there a newer release", which is a different question from
"is what I have vulnerable".

It was removed rather than upgraded: FastAPI needs it only to parse
`Form`/`File` request bodies, and none of the API's 24 endpoints accepts
one (checked against the generated OpenAPI schema, not just by grep).
Upgrading an unused parser would have cleared the finding while keeping
it in the image.

So the standing checklist is two questions, not one: *is anything out of
date* (`uv run --with pip-audit pip-audit --skip-editable`, `npm audit`)
and *is anything unused*.

## Update 2026-11-17: the npm audit covers shipped dependencies only

CI and `/gate` run `npm audit --omit=dev`. `picomatch` (GHSA-fc5q-39xc-vjpf, a ReDoS through crafted glob patterns, every
version up to 4.0.7, no patched version) reached the build tools `vite` and `vitest`, which never enter the bundle, and the only
fix npm offered was a `vite` downgrade. Turning the whole audit off would hide a shipped package's advisory; omitting
`devDependencies` keeps that check. The cost: an advisory in a build or test tool no longer fails CI, so read `npm audit`
(without the flag) now and then. `pip-audit` and `deptry` are unchanged.

`@tailwindcss/vite` and `tailwindcss` moved from `dependencies` to `devDependencies` in `frontend/package.json` (same versions;
the lockfile only gains `dev` flags): they are compile-time only, and `@tailwindcss/vite` depends on `vite`, so while it sat in
`dependencies` `--omit=dev` still audited `vite` and `picomatch`.
