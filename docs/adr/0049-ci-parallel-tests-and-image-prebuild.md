# ADR-0049: CI runs pytest in parallel and builds the images while the tests run

Date: 2026-10-25
Status: Accepted

Refines ADR-0010 (what `publish` does) and the "Verifying your work" gate in CLAUDE.md. The gate itself is unchanged.

## Context

A `main` run took about 6 minutes: `test` (about 195 s, always the last parallel job), then `publish`
(70-150 s of image builds that waited for it), then `deploy` (12 s). Measured over 45 runs
(2026-09-27 to 2026-10-24) the install, cache and queue steps were already small (2-3 s each), so the time was
in the tests and the serial publish.

Two findings explained most of it:

- **The pytest step jumped from about 90 s to about 160 s on 2026-10-13**, in one commit (the RAID unit,
  ADR-0048): 98 more tests but about 66 s more. The new tests used `RedfishFixture`, whose `shutdown()` waits
  for `serve_forever`'s next 0.5 s poll, and several hit `RedfishClient._backoff`'s real 2/4/8 s sleeps.
- **Nothing ran in parallel.** Every API and integration test used one Mongo database and one Redis keyspace
  and wiped them between tests.

## Decision

- **Tests do not sleep and do not wait for a poll.** `tests/redfish_fixture.py` serves with
  `poll_interval=0.01`, and `test_redfish_collector.py` patches `RedfishClient._backoff` (as
  `test_redfish_raid.py` already did). Sequential local time: 131 s to 51 s.
- **CI runs `pytest -n 4`** (pytest-xdist, a pinned dev dependency, 4 vCPUs on a public repo's runner).
  `tests/conftest.py` gives each worker its own Mongo database (`server-scan-test-gwN`) and Redis database
  (`/N+1`, wrapping at 15 because Redis has 16 by default). Local time with `-n 4`: 18 s. Without xdist
  (`PYTEST_XDIST_WORKER` unset) nothing changes, so a plain `uv run pytest -q` still works.
- **Coverage is only collected on `main`**, where the artifact is uploaded (about 7 s under `-n 4`).
- **A main-only `prebuild` job builds both images with `push: false`** and the same `type=gha` cache scopes as
  `publish`, in parallel with the tests. `publish` still builds and pushes, but its builds are cache hits.
  `publish` `needs` it, so the two never write one cache scope at once. Nothing reaches the registry before
  every test job is green, which is the rule ADR-0010 set.
- **Service health checks poll every second** (retries raised to 30, so the same 30 s ceiling): the services
  were only reported healthy on the first 5 s tick.
- The Playwright browser cache is keyed on the resolved `@playwright/test` version, as its comment always
  claimed, not on the whole lockfile.

## Consequences

- **A new test must not hard-code the database name or Redis `/0`.** Use the settings, or it breaks under `-n`.
  A new test that triggers a Redfish retry must not sleep for real: `_backoff` is patched in both Redfish files.
- Parallel runs are not bit-for-bit reproducible in order; a test that depends on another's side effects fails
  under `-n` and was already wrong. Local sequential runs remain the way to bisect an ordering problem.
- `prebuild` doubles the image build minutes on `main` (free on a public repo) and uses the repository's 10 GB
  Actions cache. A cache miss costs only the time saved, never a wrong image.
- **Not done on purpose:** path filtering on `main` (it would skip release and deploy for docs commits), larger
  runners (billed), merging `deploy` into `publish` (it would put the write token in a job with
  `packages: write`).

## Result (first `main` run after the change, 2026-10-24)

Run 40140549538 against the previous run 40138354832, same kind of commit:

| | Before | After |
|---|---|---|
| Wall-clock | 6m11s (371 s) | 2m21s (141 s) |
| `test` job | 195 s (pytest step about 145-160 s) | 80 s (pytest step 51 s, with coverage) |
| `publish` job | 156 s (API image 63 s, frontend 64 s) | 36 s (7 s and 6 s: cache hits) |
| `e2e` job | 88 s | 86 s |

The critical path is now `e2e` (86 s), then `publish` (36 s), then `deploy` (12 s); `test` is no longer last.
The 1 s service health interval did not visibly shorten "Initialize containers" (22 s in `e2e`); the image
pull is the likely cause, not verified. One run is one sample; the runs after it are the check against the triggers below.

## Rollback triggers

- A test fails under `-n 4` and passes alone more than once: remove `-n 4` from `ci.yml` (one line) and fix
  the shared state first.
- `publish` is not faster than before after a few runs: the cache is being evicted; drop `prebuild` and its
  `needs` entry.
