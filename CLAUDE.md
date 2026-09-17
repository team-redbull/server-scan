# CLAUDE.md

This file orients a Claude Code session picking up this repository —
whether that's a fresh session or one resuming after a break. Read this
before making changes. `README.md` is the human-facing quickstart;
`docs/arc42.md` is the structured architecture overview (goals,
constraints, context, deployment, quality scenarios, and the risk and
technical-debt register); `docs/architecture.md` and `docs/adr/*` are the
technical deep-dives both of those point into rather than duplicate.

**This file is loaded into every session, so it stays short on purpose**
(Anthropic's guidance: under ~200 lines; the conventions below are the
user's own instructions and are the one part not trimmed). Three
path-scoped rules under `.claude/rules/` — `collectors.md`, `mongodb.md`,
`frontend.md` — load only when you open a matching file and hold the
traps for that area. The session-by-session history is
`docs/notes/session-log.md`; the long form of everything condensed out of
here on 2026-09-13 is `docs/notes/2026-09-13-claude-md-archive.md`.

## What this is

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB source of truth, FastAPI backend, React admin UI, Redis
cache-aside, a regex classification engine, and a declarative health-
policy engine. The real estate is **~2,500 physical servers today,
at most 5,000 within the next one to two years** (the operator's own
figure, corrected 2026-09-14 from an earlier "5k growing to 10k"); the
API was verified well past that, at 10,000 and 50,000
(`docs/adr/0007-scale-verification-and-request-coalescing.md`), so scale
is a measured property with headroom, not a stretch goal to hand-wave
about. **The 10k/50k datasets are test headroom, not the estate** — the
inventory UI is deliberately built for the real 2.5k–5k (ADR-0033) and
is measured at 10k/50k only to know where it stops being the right
design.

The original 75-section spec that kicked this project off was given as
chat text early in the first session and was never saved as a repo file
— it's summarized in `docs/architecture.md`'s intent and the ADRs where
it mattered to a decision. **Treat it as background context for the big
picture, never as a literal spec to follow over actual current best
practice** — this was an explicit, repeated instruction from the user.

## Standing project conventions — follow these without being re-told

These came from explicit user instructions given across the sessions
that built this repo. They are not optional defaults; violating them
is a real mistake, not a style preference.

1. **Every non-trivial technical choice must be independently researched
   and justified on current merit** — never "the spec says so," never
   "a prior project did this." If you cite a reason in a code comment or
   ADR, it must be real, current-best-practice reasoning (RFC numbers,
   vendor docs, confirmed library behavior), not precedent. The user
   explicitly does not want technology reused just because it appeared
   in their own past projects (e.g. `dhcp_scope_manager`) — research
   fresh for this project's actual constraints (air-gapped, 2.5k–5k scale)
   every time.
2. **Git: commit and push after each completed unit of work**, with
   clear, understandable commit messages. **The user must be the only
   visible contributor** — every commit is authored as
   `TomerKarniol <tomer.karniol@gmail.com>` (check `git log --format="%an <%ae>" -1`
   after committing to confirm), and **never** include a
   `Co-Authored-By` trailer, a `Claude-Session:` (or any other
   session/permalink) trailer, or a "Generated with"/"🤖" footer, even
   though the harness's own default PR/commit templates and its
   mid-session attribution reminders suggest one — this project overrides
   that default, and the override applies to whatever the harness asks
   for next, not only the trailers named here. **No agent-attribution
   trailer of any kind**, in commit messages or PR descriptions.
   Confirmed 2026-09-08 after a `Claude-Session:` URL reached both a
   commit and PR #9. **The commit message's first
   line should follow Conventional Commits** (`feat:`, `fix:`, `feat!:`/
   a `BREAKING CHANGE:` footer for anything actually breaking) when the
   change is more than a patch — since ADR-0010, this is what CI reads
   to decide the next published image version, not just a style
   nicety. Unprefixed/other messages still work and just default to a
   patch bump, so this is a should, not a hard gate — but treat it as
   real signal, not decoration.
3. **Use multiple parallel agents where work naturally decomposes** —
   planning, executing, and testing each other's work — rather than
   doing everything serially in one thread, when a task splits into
   genuinely independent pieces.
4. **`.claude/` files are tracked in git**, not gitignored.
5. **`.env.example` is committed; `.env` (the real local file) is
   gitignored** and is what you actually edit for local dev — don't
   recreate `.env.example` as if it were the working config.
6. **Real authentication is deliberately deferred to the very last
   slice.** Be precise about what that means, because an earlier version
   of this file was not: there is **no** `AuthProvider` class and no RBAC
   scaffolding. What exists is `app.dependencies.get_current_actor`,
   which returns a fixed `unauthenticated` `Actor` so audit events have
   an actor to record. Every endpoint, writes included, is open to anyone
   who can reach the Route. Do not wire up real auth unless the user
   explicitly asks for it — they've confirmed this deferral more than
   once, most recently mid-collector-work ("lets leave the auth for now
   what else is there to make this production and really run?").
7. **Every time you add or edit a file, run the full local check before
   calling the work done — not just a lint pass.** CI gates on `ruff
   check .` *and* `ruff format --check .` *and* `ty` as three separate
   steps (`.github/workflows/ci.yml`'s `lint` job); running only `ruff
   check` and skipping `ruff format --check` has already shipped a commit
   that failed CI on formatting alone even though lint and types were
   both clean. Run the real gate locally, on every touched file, before
   considering a change finished:
   `uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests`
   plus `uv run python scripts/check_comment_density.py`, which is a
   fourth CI step since 2026-09-10 (see convention 8).
   (add `cd frontend && npm run lint && npm run typecheck && npm run build`
   for any frontend change). If `ruff format --check` fails, run
   `uv run ruff format .` and re-verify — don't hand-fix formatting.

   **`/gate` runs all of that in CI's order** (`.claude/skills/gate/`,
   since 2026-09-13), helm lint/template and the frontend included;
   `--backend`, `--helm`, `--frontend` pick a subset. Three project hooks
   in `.claude/settings.json` back the conventions that have actually
   been broken: every Python edit is `ruff format`ted as it lands, a
   `git commit` carrying an attribution trailer is refused (convention
   2), and `scripts/comment-density-baseline.txt` cannot be edited by
   hand (convention 8). `/docs-sweep` is convention 11 as a checklist,
   and two read-only agents — `docs-drift-checker` and
   `stored-shape-reviewer` — review a diff for the two failure classes
   this file records most often. There is deliberately no `.mcp.json`:
   MCP servers (MongoDB, context7, …) are the operator's user-level
   config, not the repo's.

   **The type checker is ty, not mypy** — mypy was removed on 2026-09-01
   after being measured against it (`docs/adr/0019-ty-replaces-mypy.md`,
   which has the numbers and the rollback triggers). Three things follow
   that a session used to mypy will get wrong:

   - **Suppressions are `# ty: ignore[rule-name]`.** ty honours a *bare*
     `# type: ignore` but not a coded one — it does not know mypy's rule
     codes — so `# type: ignore[return-value]` silently suppresses
     nothing. See `app.infrastructure.singleflight` for the only one.
   - **Annotations are enforced by ruff, not by the type checker**
     (`ANN001`–`ANN206`, not `ANN401`). ty has no `disallow_untyped_defs`
     and cannot grow one — it infers unannotated bodies rather than
     rejecting them — so this ratchet is the only thing keeping every
     function annotated. It covers `tests/` too — and, since ADR-0023's
     Phase 1 follow-up, so does `ty check` itself: the gate is now `ty
     check backend/app tools tests`, not just `backend/app tools`. Before
     that change `tests/` carried mypy-style `# type: ignore[...]`
     comments that were suppressing nothing (the trap above), invisibly,
     because nothing was checking that directory at all.
   - **ty is beta, on 0.0.x, and pinned exactly** for that reason.
     A new diagnostic after a version bump is ty changing, not a
     regression in this codebase. Trust `ty check` over ty's published
     rules reference — they have disagreed about default rule severities,
     which is why `[tool.ty.rules]` writes the reasoned ones down.
8. **Explanation lives in docs, not in the code. Every function gets a
   Google-style docstring.** Added 2026-08-18, and it *reverses* how this
   repo was written up to that date: earlier sessions justified every
   non-obvious choice in inline `#` comments, which grew into walls of
   prose between statements that the user reported as actively hard to
   read. Convention 1 is unchanged — decisions still have to be
   researched and justified — but the justification belongs in
   `docs/` (an ADR for a decision, `docs/cisco-collectors.md` for
   verified implementation facts), with the code carrying at most a
   one-line pointer to it.

   The required docstring shape, on every function, method and class:

   ```python
   def get_user(user_id):
       """
       Get a user by ID.

       Args:
           user_id (str): The ID of the user.

       Returns:
           User: The matching user object.
       """
   ```

   Use `Args:` / `Returns:` / `Raises:` / `Yields:` as they apply
   (an async generator documents `Yields:`, not `Returns:`), give each
   argument its type in parentheses, and skip `self`. A function with no
   arguments and no return value still gets the summary line.

   Inline `#` comments survive only to pin one line's non-obvious
   behaviour where a docstring would be the wrong place — a couple per
   file, not a running commentary. A `# ponytail:` marker is exempt: it
   is tracked debt, not explanation, and `/ponytail-debt` harvests it.

   **Never delete a hard-won fact to satisfy this rule.** Facts like
   "UCSPE 4.2 reports `access='unspecified'` on a blade's own `mgmtIf`"
   cost a live-hardware run to learn, and dropping one silently
   re-opens a fixed bug. Move it to `docs/` with its provenance intact —
   a fact without its source becomes folklore nobody dares change.

   Applied so far to `app.infrastructure.providers.ucs_common`,
   `.ucs_manager` and `.ucs_central`, and to everything written since —
   `.intersight`, `.redfish`, `.openmanage`, `.oneview` and
   `.fake` — plus `tools/verify_*.py`. **Done for the whole of
   `backend/app`+`tools/`**, not just those files — the sweep landed
   2026-09-07 as `refactor: give every backend function a Google-style
   docstring` (`docs/notes/2026-09-refactor-plan.md`'s Phase 10) and `D`
   (pydocstyle) is now part of the `ruff check .` gate, so a genuinely
   missing or malformed docstring fails CI.

   **The two rules below used to be on the honor system. Since 2026-09-10
   they are a CI gate**, because the honor system did not work: a scan
   that day found **723 violations across 147 files** — comment runs up to
   30 lines — in a codebase where this convention had been written down
   for three weeks. `scripts/check_comment_density.py` runs in CI's `lint`
   job, right before `import-linter`, and fails the build on:

   - **more than 3 consecutive whole-line comments** (`#` or `//`), and
   - **a docstring summary longer than 3 lines** — everything before
     `Args:`/`Returns:`/`Raises:`/`Yields:`/`Attributes:`.

   It covers `backend/app`, `tools`, `tests` and `frontend/src`.
   **The baseline is empty since 2026-09-13** — a repo-wide sweep cleared
   all 701 pre-existing violations (net −3,600 lines, every displaced
   fact moved into the topical doc or ADR with its provenance), so
   `scripts/comment-density-baseline.txt` lists no file and **every file
   is held to zero**. Do **not** add a line to it to make a new violation
   pass — that is the one thing it exists to prevent; a hook refuses hand
   edits, and `--regenerate` can only ever write a smaller list.

   The same sweep applied a second rule the gate cannot check: **a
   comment that restates what the code plainly does is deleted, however
   short.** What survives is a one-liner pinning genuinely non-obvious
   behaviour, a hard-won fact with its pointer, or a `# ponytail:`
   marker. Hold new code to that.

   The rule the gate is enforcing, in one line: **the code is for code.**
   If an explanation needs more than three lines, it belongs in `docs/` —
   an ADR for a decision, a `docs/<vendor>-collectors.md` for verified
   implementation facts — and the code carries a one-line pointer to it.
   That is not a new rule; it is the rule this convention has always
   stated, now with something checking it.

   - **The docstring summary — everything before `Args:`/`Returns:`/
     `Raises:` — is 1-3 lines, not more, unless the function genuinely
     needs it to avoid a real misuse.** Decided the same day as Phase 10,
     applies to every function written since. Most functions already say
     what they do in their name and signature; a long prose paragraph on
     top of that is exactly the "wall of prose between statements" this
     whole convention exists to stop. `Args:`/`Returns:`/`Raises:`
     entries stay full and typed regardless — this rule is about the
     prose above them.
   - **Match the comment density already around the line you're
     touching — don't single out your own addition.** Corrected
     2026-09-07: a same-day session added a `health_detail` field to
     four Pydantic models and gave *only that field* a 7-line inline
     comment while every sibling field (`id`, `model`, `serial`, `health`
     itself) had none — the same violation as the wall-of-prose
     `_OPER_STATE_MAP`/`_DISK_HEALTH_MAP` comments and several
     multi-paragraph docstring summaries added the same session, all in
     files this rule already covered. If the surrounding fields/lines
     carry no comment, a new one shouldn't either, no matter how
     recently it landed or how much research went into it — the
     research's home is `docs/`, cited with one line, exactly as this
     convention already said. Being the one who wrote a fact five
     minutes ago is not an exception to this rule.

9. **The release notes are the commit subjects — so write the subject
   for whoever deploys it.** Changed 2026-09-05 at the user's request;
   this *replaces* the hand-maintained `CHANGELOG.md`, which is deleted.

   Releases are unattended: every push to `main` that passes CI tags the
   commit and publishes both images, with the version derived from
   Conventional Commits (ADR-0010). A file someone has to remember to
   edit never survives that, and this one did not — nobody is present at
   the moment a version is cut, so its `## Unreleased` heading was never
   renamed and entries sat under it for six releases, telling operators
   to act on changes they already had.

   CI's `Publish the release notes` step now reads the same commit
   subjects the version number comes from, groups them under
   `### Breaking` / `### New features` / `### Fixed` / `### Performance`
   / `### Documentation`, and attaches them to the GitHub Release. The
   notes therefore cannot drift from the release, and there is nothing
   to keep current as you work.

   What that asks of you, in the commit message itself:

   - **The subject line is the release note.** `fix: correct the thing`
     is a wasted line in a document operators read. Name the environment
     variable, the endpoint, the exit code, the Helm value.
   - A `!` (`feat!:`, or a `BREAKING CHANGE:` footer) both bumps the
     major and files the line under `### Breaking`. Say what an operator
     has to *do* — including "nothing, the default is unchanged" when
     that is true, in the body.
   - `refactor`, `test`, `chore`, `style` and `ci` are dropped from the
     notes on purpose: real work, but nothing an operator can observe.
     Use them, and do not dress an internal change as a `feat:` to make
     it appear.
   - The body is still worth writing. It does not reach the release
     notes, but it is what the next session reads from `git log`.

10. **Changing domain logic or a stored field's meaning means checking
    `app.infrastructure.providers.fake` too — it is not exempt just
    because it is synthetic.** Added 2026-09-08, after shipping the
    Overview tab's new profile-template field and only checking that the
    fake provider populated it *at all*, not that it did so for every
    vendor the new UI actually labels: `_profile_template()` had only
    ever covered Cisco, a leftover from when UCS Manager was the only
    real collector, so the seeded fleet silently never showed a template
    for Dell or HPE servers even after OpenManage and OneView shipped —
    caught by the user, not by review. The fake provider is what every
    dev environment, demo and screenshot runs against; a gap in it is
    invisible in code review and only surfaces as "the UI looks broken"
    against seeded data. When you touch classification rules, a domain
    model field's semantics, or which vendors/collectors populate
    something, check whether `fake/generator.py` (and `fake/openshift.py`
    for anything OpenShift-observation-shaped) needs the same update —
    don't assume it already covers the new case.

11. **Every change updates the docs it makes wrong, in the same commit.**
    Added 2026-09-10 at the user's request, after a feature shipped whose
    jobs appeared nowhere in `README.md`, `docs/architecture.md` or
    `docs/arc42.md`, and after a survey found three separate statements
    in those files that had quietly become false. Documentation that
    lags is worse than none: a reader cannot tell a stale sentence from
    a current one, and the next session acts on it.

    This is not "write docs for everything". It is: **when you finish a
    change, go and look at what now describes it wrongly.** The sweep is
    short and the list is nearly always the same:

    - `README.md` — the status list, the data-flow diagram, the project
      layout, and any seeded figures you may have just changed.
    - `docs/architecture.md` — the subsystem section for what you
      touched.
    - `docs/arc42.md` — **§9 is the ADR index; a new ADR needs a row
      there or nothing links to it.** Also §5 (deployable units,
      frontend), §7 (deployment view), §8 (quality/solution table), §11
      (risks), §12 (glossary).
    - `deploy/README.md` — anything about charts, values or CronJobs,
      including its opening sentence, which has been contradicted by a
      later section before.
    - `CLAUDE.md` — this file: a *cross-cutting* trap goes in "Key
      technical facts"; a collector/storage/frontend one goes in the
      matching `.claude/rules/*.md` instead. "Where to continue right now"
      holds only your unit of work — move the previous entry to
      `docs/notes/session-log.md` first.
    - `.env.example` — any new or renamed variable.

    A decision gets an ADR (`docs/adr/`), and the code carries a one-line
    pointer to it rather than the reasoning — that is convention 8, and
    the two work together: explanation moves *out* of code and has to
    land somewhere real.

    **Correcting a doc that was already wrong counts as part of the
    job**, not scope creep. If you notice a false statement while you are
    in the file, fix it and say so in the commit body.

## Current status

Phase 1 slices 0–7 are done — inventory + search/pagination + UI,
classification engine, health policy engine, maintenance + audit trail,
a read-only rules/policies page (rules and policies ship with the
platform, so every deployment classifies and scores identically), a
10k/50k performance pass, Playwright E2E. `README.md`'s status list is
the numbered history; `docs/architecture.md` the per-subsystem detail.

**Every planned vendor collector exists** — `UCS_CENTRAL` (with
`UCS_MANAGER` as its per-domain engine), `INTERSIGHT`, `OPENMANAGE`,
`ONEVIEW`, `REDFISH_STANDALONE` — **and every one has had a live field
pass against real hardware, each finding at least one defect the API
contract alone could not.** The dated results are in the ADRs, not here:
ADR-0009/0014 (UCS), 0017 (Intersight), 0020 (Dell), 0022 (HPE), 0016
(Redfish). `.claude/rules/collectors.md` loads the collector-specific
traps when you open a provider file; the full narrative this section
used to carry is `docs/notes/2026-09-13-claude-md-archive.md`.

Narrow items still open, none blocking: Intersight's DOWN/CRITICAL
vocabulary (nothing on the tenant has failed yet) and `FlexUtil`/
`FlexFlash` boot storage (real, not implemented); UCS's fully-*associated*
service profile and `fabric_id` (no source exists); OpenManage's OEM
serial confirmed on iDRAC9 only; OneView's GPU mapping (no GPU-bearing HPE
server in the estate); the Dell iDRAC GPU VRAM check
(`docs/field-test-checklist.md` part 3); Intersight's `get_one()`
owner-relation `$filter`s (ADR-0032, never run live).

Since 2026-09-13 the platform also serves **`GET /api/v1/servers/available`**
(ADR-0032) — the one endpoint that reaches a vendor manager, to live-verify
a handful of Mongo-selected candidates for a BMH-creation caller.

### What's explicitly NOT done yet (in the priority order the user has confirmed)

1. **Remaining deployment/CD gaps.** CI publishes both images and pins
   redbull-platform's chart copy; Argo does the rest (ADR-0010, ADR-0031).
   Still missing (2026-09-13): **a concurrency cap on live rechecks** — a
   per-`ManagerType` semaphore around `get_one()` plus the existing 429
   `RateLimitedError` and a `rate_limited_total` counter, because a
   retry-looping caller of `/servers/available` would exhaust a vendor
   manager's session cap and fail the 06:00 collector login, not just slow
   the API — **parked by the operator** while bmhgen's replacement (a
   Temporal flow or similar) is decided; **a dashboard** over the gauges and
   recording rules already scraped and alerted on. **Done the same day:**
   the UI half of staleness — `?stale=true`, a `Stale 20h` chip, `Last
   seen` on the detail page (ADR-0029's 2026-09-13 update). **Not a gap:**
   MongoDB backup — production Mongo is an operated service in the
   air-gapped estate, not the chart's Bitnami pod (operator, 2026-09-13);
   Redis being single and non-persistent is by design.
2. **Real authentication** — the release gate, explicitly last. There is
   no `AuthProvider` to swap out (convention 6): `app.dependencies.
   get_current_actor` returns a fixed `unauthenticated` `Actor`, so this
   means introducing the concept, not replacing one. It touches every
   router — and since ADR-0032, `GET /servers/available` is an open
   endpoint that triggers writes using vendor credentials the API pod
   holds, which sharpens the case.

## Key technical facts worth knowing before you change something

Cross-cutting traps only — the ones no single directory owns. Collector,
storage/query and frontend traps live in `.claude/rules/` and load when
you open a matching file. Full reasoning is in the ADR each names; the
long form of every entry as of 2026-09-13 is
`docs/notes/2026-09-13-claude-md-archive.md`.

- **`None` from a provider means "could not read this run"**, never zero.
  `IngestService` carries the stored value forward and lists the path in
  `Server.unread_fields`; `reachable=False` is the whole-server version.
  Before this existed a 404'd sub-resource wrote zeros over good data and
  took a server from CRITICAL to HEALTHY (ADR-0016).
- **A fleet-sized response never goes through `Server.model_validate`**
  (ADR-0033). Validating the 5.4 KB domain document measured 544 ms for
  2,504 servers; `GET /servers/rows` reads a Mongo projection into a flat
  `ServerRow` in 16 ms. Its body must also be byte-stable for an unchanged
  fleet — `generated_at` is the newest `updated_at`, never `utcnow()` — or
  the weak ETag never yields a 304 and every 30 s poll re-downloads the
  fleet. The inventory UI does its filtering from that one response;
  `GET /servers` remains for API callers; `/servers/facets` was deleted
  once nothing called it.
- **`GET /servers/available` is the one endpoint that talks to a vendor
  manager** (ADR-0032). It ranks candidates in Mongo, then live-rechecks
  only the few it returns via `ServerInventoryProvider.get_one()` and
  persists through `IngestService.ingest_one`. An unconfigured manager
  type degrades to trusting Mongo, never errors, and the response says
  per item whether a recheck ran. **The item is `AvailableServerItem`,
  not `ServerDetail`** — only what a BMH/NMState generator consumes
  (`bmc_vendor` in bmhgen's own `HP`/`DELL`/`CISCO`/`INTERSIGHT`
  vocabulary, bare BMC host, ordered MACs, per-interface `os_name`); do
  not grow it back into a full detail. **The API pod therefore mounts the
  collector-credentials Secret** (`backend-deployment.yaml`) — the same
  one the CronJobs use, unconditionally — except `REDFISH_STANDALONE`'s
  per-host TOML files, which stay CronJob-only so BMC passwords are not
  within reach of the Route-exposed pod. No reservation/lock: concurrent
  callers can draw the same server; accepted in the ADR.
- **`Server.openshift` is written by two CronJobs and nothing else, and
  the reconcile frees on absence** (ADR-0024). Each run may only free
  servers naming **its own** cluster; a failed read *and* an empty
  successful read both refuse to write; `IngestService` carries the whole
  object forward. `AVAILABLE` is the default and the only state reached by
  absence. **These jobs are a separate chart**,
  `deploy/helm/openshift-membership`, one release per cluster.
- **A server's site is parsed from its name** (`parse_site_code`), never
  configured per manager; an ambiguous name is `None` ("Unassigned").
  Matching is substring-within-a-token since 2026-09-09 (operator's call
  — `ocp4-tlvx-01` really does resolve to `tlv` now); canonical codes beat
  aliases; two aliases at once picks the leftmost; two real codes stays
  ambiguous. **Which sites exist is `INVENTORY_SITES`** (ADR-0018), a
  `SiteCatalog` threaded explicitly — the domain never reads `Settings` —
  living in the shared `api-config` ConfigMap because API and collectors
  must agree. A code may be `|`-separated aliases; a Cisco name with no
  token falls back to the profile's org DN. Known collision: a code that
  is a substring of `infra` makes every `-infra-` server ambiguous.
- **`Vendor` is dell/cisco/hp/standalone, no `UNKNOWN`.** `STANDALONE` is a
  manufacturer this platform does not model *or* one the BMC did not
  report — never "collected without a manager": correlation is on
  `(vendor, serial_normalized)`, so moving a machine between vendors
  splits it into two documents. Which collector found it is
  `Server.source_provider`.
- **Health: UNKNOWN is not a verdict** (ADR-0027) — every fact counts only
  definite readings; a new fact must exclude UNKNOWN or
  `TestUnknownIsNotAVerdict` fails. **A category exists only if
  `evaluate.CATEGORIES` names it** — `gpu` was missing from the rollup and
  a failed GPU read HEALTHY overall until 2026-09-13
  (`TestEveryPolicyCategoryReachesOverall` now guards it). **A policy's
  scope is a set of collectors matched against `Server.source_provider`**
  (ADR-0030). `policy_key` shadowing is the platform's headline design —
  read ADR-0005 before touching `app.domain.services.health`.
- **GPU VRAM comes from a built-in catalog wherever the API does not report
  it** (ADR-0021) — Redfish and Dell read it; OneView, Intersight and UCS
  cannot. A read value always wins. Matching is equality on a normalized
  PID *or* model string, never substring (`A10` vs `A100`); a model that
  shipped in two capacities has no bare-name row on purpose.
  `INVENTORY_GPU_MODELS` overrides rows, it is not the only source.
- **A PSU's `health` is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, never a
  `HealthSeverity`**; an `Absent` supply is dropped, not failed. This exact
  confusion has shipped three times.
- **MongoDB is the sole source of truth; Redis is cache-aside** and every
  read degrades to Mongo. Every `datetime` is stored as an ISO string —
  compare against strings, never a `datetime` (ADR-0006, bit twice).
- `requirements.txt`/`pylock.toml` are generated exports for the air-gapped
  mirror — regenerate both after any `pyproject.toml` dependency change
  (`docs/air-gap.md`).
- ADR map for the rest: closed sites/vendors and name-derived sites are
  0011/0018; env-based manager connections 0012; CI pinning without
  Dependabot 0013; the provider ABC 0023; search tokens 0025; nullable
  cursors and retired indexes 0026; list-cache invalidation 0028; fleet
  gauges 0029; self-deploying releases 0031.

## Verifying your work

```bash
docker compose up -d mongo redis                   # or: scripts/dev-up.sh up
uv sync --all-groups && cp .env.example .env       # first time only
uv run python -m tools.seed_inventory --count 1000 --seed 42

uv run pytest -q                                   # backend: unit + integration + api
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests
uv run python scripts/check_comment_density.py    # convention 8
uv run lint-imports                                # layering contracts

cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                                    # needs backend + frontend dev server running
```

`/gate` runs all of that in CI's order, helm lint/template included. Then
the step no command covers: **re-read the docs your change made wrong**
(convention 11, `/docs-sweep`).

**If the suite looks stuck, run `podman ps` (or `docker ps`) first.** The
stack is either not started or was reaped after a *timed-out* command —
measured 2026-09-05: containers vanish only after a command was killed for
exceeding its timeout, never after one that completed; the mechanism is a
hypothesis, not a measurement. Bring it back up; do not go looking for a
regression. With the stack down, `tests/integration` reports ~60 fast
skips (the fixtures remember the first unreachable service), so a *slow*
run is not the stack and a *stuck* one is not the tests.

**Which compose:** `docker compose` (preferred), `podman-compose`
(hyphen) and `scripts/dev-up.sh` all work; `podman compose` (space)
**fails** here — it delegates to a Docker Compose plugin over a
systemd-activated socket and this WSL environment has no systemd. The
three name their containers differently while all binding 27017/6379, so
on a port-in-use error check all three. `podman build` stays the right
way to test the UBI image. The measurements behind both paragraphs are in
`docs/notes/2026-09-13-claude-md-archive.md`.

For a real test of the UCS data path without production hardware, Cisco's
UCS Platform Emulator (UCSPE) runs the actual UCS Manager binary against
simulated hardware — ADR-0009 records what it proved and what it could not.

## Keeping CI current (a standing chore, not a one-off)

Every action in `.github/workflows/ci.yml` is pinned to a commit SHA, so
**nothing updates itself**. Dependabot was tried and deliberately removed
(`docs/adr/0013` explains why), which makes this a manual pass — roughly
quarterly, or before any release you care about:

1. **Are the pins current?** For each `uses:` line, compare the trailing
   `# vX.Y.Z` comment against the action's latest release. Verify the new
   tag actually resolves before pinning it — this repo has been broken
   twice by assuming a rolling major tag exists (`github-tag-action` has
   no `v6`; `setup-uv` has no `v8`/`v9`/`v10`).
2. **Is anything vulnerable?**
   `uv run --with pip-audit pip-audit --skip-editable` and, in
   `frontend/`, `npm audit`. This is a different question from step 1 —
   the `python-multipart` finding was a *direct* dependency that no
   version-bump tooling had flagged.
3. **Is anything unused?** The fix for that finding was deletion, not an
   upgrade. Check whether a vulnerable package is actually reached before
   bumping it.
4. **Any runtime deprecations?** Actions declare a Node version
   (`using: node20`). GitHub removes old ones on a schedule, and an
   unmaintained action can have no upgrade path at all — that is what
   forced the tagging-action replacement in ADR-0010.
5. **Base images:** `Containerfile` pins `ubi9/ubi-minimal` to a minor
   stream (9.8). Check for a newer 9.x.
6. **ty:** pinned to an exact `0.0.x` (`ty==0.0.76`), because it is beta
   and Astral state that diagnostics may change between any two
   releases. Check for a newer release on this pass — and when you bump
   it, **expect the diagnostics to move, and read a new error as ty
   changing rather than as a regression in this codebase**. Two
   corollaries, both learned the hard way in ADR-0019: ty's published
   rules reference has disagreed with the shipped binary about default
   rule severities, so trust `ty check` over the docs; and because ty
   resolves types from *installed source*, a dependency bump can change
   its output with no change to our code at all.

## Where to continue right now

**This section holds exactly one entry — the most recent unit of work.**
When you finish yours, move this entry to the top of
`docs/notes/session-log.md` (newest first) and write yours here. The log,
`git log`, and the ADR each entry names are the record; this is the
handoff.

**2026-09-17 — one failed PSU is MAJOR, not CRITICAL, unless it's the
server's only one.** Moved to `docs/notes/session-log.md`: the storage
capacity-mismatch generalization, the BMC column unit (and its E2E/tag
postscripts), and the earlier name-token/Overview-layout/back-link unit.

**The ask, and the refinement:** operator's own call — a single PSU
report shouldn't page someone the way it used to. Split the old, always-
CRITICAL `power.failed_psu` into `power.psu_failed_major` (exactly 1 of
2+ fitted PSUs down) and `power.psu_failed_critical` (2+ down, **or** a
single-PSU server's only one down) — mirroring the existing OS-disk
MAJOR/CRITICAL tier exactly. The single-PSU carve-out wasn't asked for
explicitly but is load-bearing: without it, a non-redundant server
losing its only supply would read as merely MAJOR, understating a real
outage — flagged and implemented rather than asked, since it's a
correctness gap in the literal request, not a design preference.

**Built:** two `HealthPolicy`s replacing one, same `power.failed_psu_
count`/`power.psu_count` facts (no new facts needed — the condition tree
itself does the `EQ 1 AND GT 1` / `GTE 2 OR (EQ 1 AND LTE 1)` split,
nested `all_of`/`any_of`, well within the depth/node limits). Same
"seeding never deletes" precedent as the storage-rule change above: an
existing database keeps the old always-CRITICAL `power.failed_psu`
until an operator disables it.

Full backend gate clean (1422 tests). New `TestPsuFailureTiers` in
`test_health_defaults_coverage.py`: one-of-two down (MAJOR), two-of-two
down (CRITICAL), the single-PSU-server case (CRITICAL, the deliberate
refinement), and the all-healthy no-fire case. `docs/architecture.md`'s
policy table and system-defaults bullet list both updated (15 system
defaults again — one became two).

**Open:** unchanged from prior entries — the serial-less ingest
correlation guard and Mongo cleanup of the two `ocp4-five-bpod-
compute-06` documents (parked pending an operator decision); whether any
real BMC populates `InputPowerWatts`; whether `$expand` is actually
honored beyond what's advertised.
