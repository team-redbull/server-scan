# CLAUDE.md

This file orients a Claude Code session picking up this repository —
whether that's a fresh session or one resuming after a break. Read this
before making changes. `README.md` is the human-facing quickstart;
`docs/arc42.md` is the structured architecture overview (goals,
constraints, context, deployment, quality scenarios, and the risk and
technical-debt register); `docs/architecture.md` and `docs/adr/*` are the
technical deep-dives both of those point into rather than duplicate.

## What this is

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB source of truth, FastAPI backend, React admin UI, Redis
cache-aside, a regex classification engine, and a declarative health-
policy engine. The real estate is **~5,000 physical servers today,
growing to up to 10,000** (the operator's own figure, 2026-09-13); the
platform was verified well past that, at 50,000
(`docs/adr/0007-scale-verification-and-request-coalescing.md`), so scale
is a measured property with headroom, not a stretch goal to hand-wave
about.

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
   fresh for this project's actual constraints (air-gapped, 5k–10k scale)
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

   It covers `backend/app`, `tools`, `tests` and `frontend/src`. The 701
   pre-existing violations are recorded in
   `scripts/comment-density-baseline.txt` with a per-file allowance.
   **That file may only ever shrink.** A file listed there may not get
   worse; a file not listed there may not have a single violation. Do
   **not** add a line to it to make a new violation pass — that is the
   one thing it exists to prevent. When you clean a file up, run
   `uv run python scripts/check_comment_density.py --regenerate` to bank
   the improvement.

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
    - `CLAUDE.md` — this file: "Key technical facts" for a new trap, and
      "Where to continue right now" for what you just finished.
    - `.env.example` — any new or renamed variable.

    A decision gets an ADR (`docs/adr/`), and the code carries a one-line
    pointer to it rather than the reasoning — that is convention 8, and
    the two work together: explanation moves *out* of code and has to
    land somewhere real.

    **Correcting a doc that was already wrong counts as part of the
    job**, not scope creep. If you notice a false statement while you are
    in the file, fix it and say so in the commit body.

## Current status

Phase 1 slices 0–7 are done (see `docs/architecture.md`'s "What's
implemented vs. planned" section for the full per-slice writeup):
inventory + search/pagination + UI, classification engine, health policy
engine, maintenance + audit trail, classification/health UIs (since made
read-only and merged into one page — rules and policies ship with the
platform, so every deployment classifies and scores identically), a
10k/50k performance pass, and Playwright E2E coverage.

Beyond the numbered slices, the **first real vendor collector — Cisco UCS
Manager** — is built, and has now been **validated end to end against a
live Cisco UCS Platform Emulator** (UCSPE 4.2(2aS9)): full collector run,
then the REST API and UI over the result.
`docs/adr/0009-ucs-manager-collector.md` records what that proved, what
it disproved, and what it still could not settle. Several defects it
found would have been invisible without real hardware — a nonexistent MO
class that aborted every run, a BMC filter that matched nothing, a whole
class of adapter interface never collected, fabric path counts that were
always zero, and servers named after their chassis slot rather than
their service profile (which silently defeated both site parsing and
classification).

Also since: vendors are a closed enum and sites a closed set loaded
from configuration, a server's site is parsed
from its own name, vendor manager connections come from environment
configuration rather than MongoDB documents plus mounted secrets, and the
UI was rebuilt around a per-site overview as the landing page — which now
leads with fleet-wide cards (across all sites, UPI, MCE, hosted cluster)
above the per-site ones, summed from a `by_installation_type` object
`GET /api/v1/sites` returns per site row. **`InstallationType` gained a
fourth member, `MCE`, 2026-09-08** — an MCE hub's own nodes are pulled
out of the generic UPI bucket into their own card, and all four system-
default classification rules became broad, overlapping, order-dependent
prefix/substring catch-alls rather than mutually exclusive by
construction (`app.infrastructure.mongodb.classification_rule_repository.
default_system_rules`) — read that module's own comment before touching
it, the ordering is load-bearing now in a way it wasn't before. **UPI's
own pattern became unconditional (`.*`) 2026-09-10** — it used to require
an `ocp4` prefix; now it matches every name that isn't already claimed by
a hosted-cluster or MCE rule, so `InstallationType.UNCLASSIFIED` is
unreachable through the system defaults (it still exists as the enum's
zero value and as what a fully custom ruleset can produce).

**Every planned vendor collector now exists.** Cisco Intersight
(ADR-0017), Dell OpenManage (ADR-0020) and HPE OneView (ADR-0022) all
shipped after UCS, alongside `REDFISH_STANDALONE` for machines no
aggregator owns. **`ONEVIEW` was validated against a live appliance on
2026-09-07** (821 servers) — see ADR-0022's "Results, 2026-09-07" for
what it settled and the storage-mapping bug it found and fixed the same
day. `INTERSIGHT` has still never had its field mappings run against live
hardware, which remains the outstanding action on the repo. Three
platform-wide changes landed with that work and are worth
knowing before reading any collector: a **built-in GPU catalog**
(ADR-0021) fills in VRAM the Cisco and HPE APIs do not report (Redfish
does, so a real reading wins), **`Server.unread_fields`**
records what a collection could not read, and **every collector now
reports power supplies**, so the health engine's `power.*` metrics
finally have something to read. All three are in "Key technical facts"
below.

The supply-chain pass after that (`docs/adr/0013`) SHA-pinned every CI
action, replaced the release-tagging action before Node 20 removal breaks
it, moved the base image from UBI 9.4 to 9.8, and removed
`python-multipart` — an unused direct dependency carrying seven CVEs.
`pip-audit` and `npm audit` are both clean as of that commit. **It also
left a standing obligation: see "Keeping CI current" below.**

### The collector architecture (read this before touching a collector)

There is no single sync process. Each hardware vendor gets its own
`ServerInventoryProvider` implementation
(`app.infrastructure.providers.<vendor>`, following the seam
`app.domain.ports.provider` defines and `app.infrastructure.providers.
fake` — the Phase-1 synthetic-data provider — already exercises), and
each manager *type* gets its own Kubernetes `CronJob` running
`tools/run_collector.py --manager-type <TYPE>`. A run:

1. Resolves that type's endpoint + login from settings via
   `app.infrastructure.credentials.env.EnvConnectionResolver`
   (`INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD`, same shape for
   `ONEVIEW`, `OME`, `INTERSIGHT`). **One endpoint and one login per
   manager type — that is the whole connection config.** There is no
   `Manager` document to create and no credentials directory to mount;
   both were removed. A half-configured vendor raises
   `ManagerNotConfiguredError` naming the missing variables.

   **`UCS_MANAGER` is the one carve-out: a login with no endpoint.**
   `INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` exist,
   `INVENTORY_UCS_MANAGER_IP` does not, and there is no UCS Manager
   collector to run. The UCS Central collector discovers every domain's
   address from Central at runtime and logs into each one with that
   account, so an endpoint here would name a single domain that nothing
   reads. See the Cisco section below.
2. Talks to the vendor API, normalizes into `ProviderServer`.
3. Runs that through `app.application.services.ingest.IngestService` —
   the exact same pipeline the fake-data seeder and every other
   provider use: classify, health-evaluate, audit, upsert, one write per
   server.

A `Manager` document is still written on each run, but it is a
*projection* of that configuration (`tools.run_collector.manager_for`)
so the API can resolve `Server.manager_id` to something readable — never
its source. Intersight reuses the same three fields with different
meanings: it signs requests with an API key, so `username` is the API Key
ID and `password` the secret key.

A collector never talks to the FastAPI process; the API never talks to a
vendor manager. MongoDB is the only thing connecting them. See
`README.md`'s diagram and `docs/adr/0009-ucs-manager-collector.md`, whose
validation sections record what a live UCS Platform Emulator proved,
disproved and could not settle.

**Five collectors exist: `UCS_CENTRAL` (the UCS-managed Cisco fleet),
`INTERSIGHT` (Cisco servers no UCS domain owns), `OPENMANAGE` (Dell),
`ONEVIEW` (HPE) and `REDFISH_STANDALONE` (every machine no aggregator
owns).** Every `ManagerType` now has an entry in
`tools/run_collector.py`'s `PROVIDER_FACTORIES` **except `UCS_MANAGER`,
whose absence is deliberate rather than pending** — it is reached through
`UCS_CENTRAL`, which discovers each domain's address at runtime, so there
is nothing to point a CronJob at. The tool says exactly that rather than
reporting an unimplemented feature.

**`INTERSIGHT` is the first collector that actually reaches the 10,000-server
ceiling**, and the first with three properties nothing else here has —
read `docs/adr/0017-intersight-collector.md` before touching it:

1. **It is not a login.** Intersight has no username/password path for
   its REST API at all; every request is signed (HTTP Signature
   `hs2019`). Its credential variables are named for what they are —
   `INVENTORY_INTERSIGHT_API_KEY_ID` and `_API_KEY_PEM`, not the
   USERNAME/PASSWORD pair every other vendor takes. The PEM rides in the
   environment variable — the signing library takes the key as a string,
   so there is **no key file to mount** and ADR-0012's rule holds.
   Signing is hand-rolled on `httpx` + `cryptography` rather than using
   the official SDK, which is a 57.6 MB wheel of 10,112 generated model
   modules for the eight we would touch. The RSA construction was
   verified byte-identical against that SDK.
2. **Its cost is flat in fleet size.** Every child managed object carries
   an inverse reference to its owner, so each sub-resource is listed once
   for the whole estate and joined in memory — ~120 requests for 10,000
   servers. The trade is memory: the join tables are held for the length
   of the run and scale with the fleet, which no other collector's do.
   `$select` on every query is what keeps that affordable, not a
   micro-optimisation.
3. **It deliberately does not collect `ManagementMode == UCSM`.** Those
   are exactly the servers `UCS_CENTRAL` already owns, and since
   `IngestService` correlates on `(vendor, serial_normalized)`,
   collecting both would make one document's `source_provider` and every
   mapped field flip on whichever CronJob ran last.
   `INVENTORY_INTERSIGHT_MANAGEMENT_MODES` overrides it, for an estate
   whose UCS domains are not registered with Central at all.

**No longer unverified, as of 2026-09-07 — this used to say it had never
been run against a live Intersight; it now has, several times, against
the user's on-prem Private Virtual Appliance, including
`--manager-type INTERSIGHT --dry-run` itself, not just the probe.** The
DevNet sandbox is still offline (went dark 2026-08-01, no committed
return before ~Q1 2027) and there is still no downloadable emulator
equivalent to UCSPE, so the mapping was still *built* against the
OpenAPI contract rather than a test target — but live field passes
across 2026-09-01 and 2026-09-07 (`tools.verify_intersight` and
`--dry-run` against a real, if small, ~20-server tenant) have since
confirmed and fixed five real defects the contract alone couldn't have
caught: a `ComputeBoard`-only join gap that zeroed out storage and
`cpu_model`, a GPU catalog matcher that couldn't recognize Intersight's
own product-name spelling (`"NVIDIA T4 PCIe 16GB 70W"`), and PSU health,
GPU health/NIC `oper_state`, and drive health all silently reading
UNKNOWN because Intersight's `"OK"` string had no entry in either
`normalize_oper_state` or `_drive_health` — see ADR-0017's "second field
pass" section for the full write-up. **`TotalMemory`'s unit is SETTLED:
MiB**, confirmed against the Intersight UI's own "Memory Capacity" figure
to the decimal (`786432 ÷ 1024 = 768.0` GiB exactly). **What's still
genuinely open**, none of it blocking: boot-optimized storage
(`FlexUtil`/`FlexFlash`) is confirmed real on this tenant but **not
implemented**; the DOWN/CRITICAL counterpart to Intersight's `"OK"`
vocabulary is unconfirmed on both fields above, since nothing on this
tenant has actually failed yet to check it against; and a handful of
smaller ADR-0017 UNVERIFIED-list items (CPU-name field disambiguation,
BMC address precedence, clock-skew behavior, account region) remain
exactly that — unverified, not urgent.

An air-gapped site reaches Intersight **only** through an on-prem
Intersight; `intersight.com` is public internet and a *Connected* Virtual
Appliance still calls home. The user has one reachable from the
air-gapped environment (not the flavour Cisco brands a "Private Virtual
Appliance" — the product ships under several names). **So this collector
is testable there, and its first real run is the outstanding action**:
`docs/field-test-checklist.md` says exactly what to run and what to bring
back — three exported variables and `uv run python -m
tools.verify_intersight`. The `TotalMemory` unit is the answer to look
for.

**`OPENMANAGE` (Dell) is the one collector that reads from two places on
purpose**, and the split is on *provenance*: two bulk calls to the
OpenManage Enterprise appliance say which servers exist and what the
operator named them, then each server's own iDRAC is read over Redfish
(reusing `..providers.redfish`, not a second mapping) for the measured
hardware. It is therefore the only collector needing two logins —
`INVENTORY_OME_USERNAME`/`_PASSWORD` plus
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for a shared read-only iDRAC
account — and it refuses to start without both, naming the variables.
See `docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md` and
`docs/dell-collectors.md`.

**Validated against a live OME appliance and several iDRAC9 servers on
2026-09-08**, and it found the same shape of defect every other
collector's first live pass has found: a field that looked right against
the contract and was wrong against real hardware. `ComputerSystem.
SerialNumber` — assumed since ADR-0020 to be Dell's Service Tag, and
flagged there as "the highest-consequence unverified assumption in the
design" — is a manufacturing/board serial, not the Service Tag OME shows
as `DeviceServiceTag`. The real one is `Oem.Dell.DellSystem.NodeID`, a
Dell OEM extension `mapping._dell_serial` now reads in preference to
`SerialNumber`. Fixed the same day; see ADR-0020's "Status of
verification" and `docs/dell-collectors.md`'s "Collection flow" for the
full writeup and what a wrong-but-stable serial would have done to
correlation if it had shipped unfixed.

**A second, related field bug hit production 2026-09-10**:
`TargetName`/`DeviceName` — read as the iDRAC address since ADR-0020 —
are display names OME derives per its own "Server Device Naming" console
setting, and when that setting is System Hostname (not iDRAC Hostname)
they hold a server's OS hostname instead, causing
`unreachable, could not reach <hostname>`. `mapping._network_address` now
prefers `DeviceManagement[0].NetworkAddress`, which every one of Dell's
own OME automation scripts uses to reach a device and which is immune to
this setting; `TargetName`/`DeviceName` remain the profile-to-device join
key (unaffected — both sides move together under either naming mode) and
the address fallback when a device has no `DeviceManagement` entry. See
ADR-0020's dated update and `docs/dell-collectors.md`'s "Collection flow".

**A genuinely unreachable iDRAC is now a visible server document, not
just a PARTIAL exit code — `OPENMANAGE` only, so far.** Same 2026-09-10
incident: OME already knows a profile's identity before its BMC is ever
contacted, so a plain connection failure now yields a `reachable=False`
placeholder (`OpenManageProvider._unreachable_server`) instead of nothing.
`IngestService` treats `reachable=False` like any other unread field —
carries the server's last-known hardware forward, never blanks it — and
sets `Server.reachable`/`unreachable_since`. This one failure mode no
longer counts toward `collection_errors`, so the CronJob pod exits 0 for
it. See `docs/dell-collectors.md`'s "Collection flow" and the
`Server.reachable` entry below.

**`REDFISH_STANDALONE` shares the exit-code relaxation, not the Mongo
document.** Same day, same shared `..redfish.provider._collect_host`: a
plain connection failure now logs at ERROR (was WARNING) and is excluded
from the PARTIAL decision. It stops there — a standalone target's
inventory file carries only a host and an optional name, never a serial,
so there is no stable `(vendor, serial_normalized)` key to write a
placeholder against without risking a new duplicate document on every
unreachable run. See ADR-0016's dated update for exactly why, and what
closing that gap would require.

**Widened the same day to cover auth failures too, both collectors**,
after a real OME run hit rejected credentials often enough that PARTIAL
stopped meaning anything unusual: a rejected login is exit-0-safe, via
`tools.run_collector._is_benign_collection_error` and
`..redfish.provider.AUTH_REJECTED_MARKER`. **Still PARTIAL-worthy**: TLS
failures, a per-host time budget exceeded, and any other unrecognized
error — none of those are auth/reachability outcomes in the same "happens
on a normal Tuesday" sense. See ADR-0016's second 2026-09-10 update.

**Nothing skips a BMC any more, since 2026-09-12** — `_AuthGuard` is
deleted, with `INVENTORY_REDFISH_AUTH_FAILURE_THRESHOLD`/`_BUDGET`, the
two Helm values, and the `AUTH_CREDENTIAL_DISABLED_MARKER`/
`AUTH_BUDGET_EXHAUSTED_MARKER` shapes. Every host in the inventory is
attempted every run however many earlier hosts rejected the same
credential; each rejection logs `redfish.bmc_login_failed` at ERROR and
the run summary carries `auth_failures`. **The lockout risk it covered is
real and is now the operator's** — re-running with a wrong shared
credential locks Lenovo XCC accounts and IP-blocks this collector from
every iDRAC for an hour. Do not re-add a breaker without asking; this was
an explicit operator decision (ADR-0016's 2026-09-12 update).
**`OPENMANAGE` goes one further: a rejected login now writes the same
`reachable=False` placeholder a dead BMC does**
(`..openmanage.provider._is_uncollected`, which replaced
`_is_unreachable`). `REDFISH_STANDALONE` still writes none — no serial to
correlate against.

**`ONEVIEW` (HPE) deliberately does *not* copy that split, and this is
the thing a future session is most likely to get wrong.** The estate runs
iLO 4, 5 and 6 in the same racks, and iLO 4 predates useful Redfish
coverage. A Dell-shaped design would therefore mean a per-generation
branch in the collection path and two different sets of field provenance
for one vendor's servers in one inventory — "why does this server have
thread counts and that one doesn't" becomes a question about which branch
ran. **So the user decided, explicitly, on one collection standard for
all HP hardware: OneView, for every server, whatever its iLO
generation.** It is not up for re-litigation in code. Concretely: no
Redfish pass, no `RedfishTarget`, no BMC credentials, no
`INVENTORY_ONEVIEW_BMC_*`, and `mpModel` is read and *reported* but never
branched on. What OneView cannot report is `None` — "not read this run" —
never zero. Read `docs/adr/0022-oneview-only-hpe-collector.md` and
`docs/hpe-collectors.md` before touching it.

The cost is three bulk calls per appliance — `GET /rest/server-hardware`
returns the *complete* object per member rather than a summary, and
`expand=all` folds in each server's DIMMs, drives, GPUs and PCI devices.
Power supplies and, since 2026-09-07, CPU thread counts are the two
potentially-per-server calls — each tried the cheap way first (most
servers' `expand=all` response already carries both), falling back to a
bounded per-server call and independently switchable off
(`INVENTORY_ONEVIEW_COLLECT_PSUS`/`_PSU_CONCURRENCY`,
`INVENTORY_ONEVIEW_COLLECT_CPU_THREADS`/`_CPU_THREADS_CONCURRENCY`).
`/processors` is OneView's only source for `cpu_threads` —
`server-hardware`'s own fields carry no thread count, unlike every other
collector, which gets one for free on data already fetched.

**Validated against a live appliance on 2026-09-07** (821 servers, 685
profiles, iLO 5 and iLO 6 both present) — `uv run python -m
tools.verify_oneview` was the outstanding action, and now has been run.
Both headline questions are settled: `processorCount *
processorCoreCount` matched the real core count on every sampled server,
and the `/rest/server-profiles` 256 cap is **per request**, not per
query — paging fetched all 685 profiles, so an estate over the cap is
still fully enumerable with no sharding needed. The run also found a real
bug, fixed the same day: `LocalStorage` (the v1 storage schema) was
mapping every server to zero drives, because `LocalStorage.data` is a
list of per-controller objects (each with its own `PhysicalDrives[]`),
not a flat drive list, and a separately-empty `LocalStorageV2` read
wasn't falling back to v1 at all. See ADR-0022's "Results, 2026-09-07"
for the full write-up and the two open questions it could not settle
(both GPU-related — this estate has no GPU-bearing HPE server).

### What's explicitly NOT done yet (in rough priority order the user has confirmed)

0. ~~Staleness detection~~ — **done 2026-09-12, ADR-0029**, kept as a
   standing note. `/metrics` now carries gauges the API derives from
   MongoDB on scrape: `server_scan_servers_stale{source_provider}`,
   `server_scan_collector_last_seen_timestamp_seconds{source_provider}`,
   `server_scan_cluster_last_reported_timestamp_seconds{cluster}` and
   friends, with a `ServiceMonitor` and four alerts in the chart. **What
   is still open is the UI half** — a `stale` filter on the inventory —
   and per-run collector counters (duration, ingested, errors), which a
   scrape-derived gauge cannot carry and would need a push mechanism.
1. ~~Live-hardware validation~~ — **done for all five collectors as of
   2026-09-08**, kept here as a standing note rather than deleted, the
   same as the seeded-shape item at the bottom of this file.
   `UCS_MANAGER`/`UCS_CENTRAL` against UCSPE and, as of 2026-09-07, a real
   air-gapped domain too (ADR-0009's dated "Update" sections); `ONEVIEW`
   against a live appliance (ADR-0022's "Results, 2026-09-07" — a real
   storage-mapping bug found and fixed the same day); `INTERSIGHT`
   against the user's on-prem Private Virtual Appliance, also 2026-09-07
   (ADR-0017's "second field pass" — five real defects found and fixed,
   from a `ComputeBoard` join gap to `"OK"` reading UNKNOWN across
   PSU/GPU/drive health); and, closing the list, `OPENMANAGE` against a
   live OME appliance and several iDRAC9 servers on 2026-09-08 (ADR-0020's
   "Status of verification" — the Service Tag correlation-key assumption
   the ADR itself flagged as highest-consequence was wrong, found and
   fixed the same day: real Service Tag is `Oem.Dell.DellSystem.NodeID`,
   not `ComputerSystem.SerialNumber`). Every one of the five found at
   least one real defect a live run alone could surface — that pattern is
   now five-for-five. Narrower open items remain, none blocking:
   Intersight's DOWN/CRITICAL `OperState`/`Health` vocabulary (needs a
   genuinely failed component to check against, not a rerun), UCS's
   fully-*associated* service profile (nothing tested has gone past
   `config-failure`), and OpenManage's Dell OEM serial fix being confirmed
   only on iDRAC9 (iDRAC7/8 may shape the OEM block differently).

   **The research bar for any future vendor work is unchanged**, so it
   is kept here rather than deleted with the item it belonged to:
   research that vendor's *current* API docs directly, and don't trust
   this file's or any older research's specifics without reconfirming
   them. UCS Manager's build read Cisco's official XML API guide and
   cross-checked every attribute name against the actually-installed
   `ucsmsdk` package source rather than trusting documentation
   summaries; OneView's read HPE's API Reference and the `hpeOneView`
   SDK's source for the four behaviours a hand-rolled client learns the
   hard way. Hold that bar. Testability without real hardware varies a
   lot by vendor and was the deciding factor for going UCS-first — two
   of the four vendors turned out to have no test target at all, which
   is why this item exists.
2. **Remaining deployment/CD gaps.** Corrected 2026-09-13 — this item
   used to say the frontend had no manifests and there were no alert
   rules; both exist now. What is true today: CI publishes both images to
   GHCR on every push to main (`.github/workflows/ci.yml`'s `publish`
   job, `docs/adr/0010-image-publishing-and-versioning.md`), and the
   platform is deployed by Argo CD from `team-redbull/redbull-platform`
   (`gitops/charts/server-scan`, a copy of this chart with that cluster's
   overrides on top). **The image tag there is a hand-edited pin** (`1.0.0`
   as of 2026-09-13), not bumped by CI — the org's `ghcr-build-push.yml`
   Helm-bump flow, which other services already use, is the obvious
   wiring. Still missing: rate limiting (`RateLimitedError` and the 429
   mapping exist, nothing raises them); a MongoDB backup — it is a single
   Bitnami pod with a 20Gi PVC holding the whole source of truth, and a
   `mongodump` CronJob is the minimum; and a dashboard over the 20 gauges
   and 18 recording rules that are already scraped and alerted on. Redis
   being single and non-persistent is by design (cache-aside, degrades to
   Mongo), not a gap.
3. **Real authentication** — the release gate, explicitly last. There is
   no permissive `AuthProvider` to swap out (convention 6 above says why
   an earlier version of this file was wrong about that): what exists is
   `app.dependencies.get_current_actor` returning a fixed
   `unauthenticated` `Actor`, so building this means introducing the
   concept, not replacing one. It touches every router.

## Key technical facts worth knowing before you change something

Full detail lives in `docs/adr/`; this is just the index of what's
non-obvious enough to bite you.

- **`Server.openshift` is written by two CronJobs and by nothing else,
  and the reconcile frees on absence.** Read
  `docs/adr/0024-openshift-cluster-membership.md` before touching
  `app.application.services.openshift_membership`. Three things bite:
  each run may only free servers already naming **its own** cluster (that
  scope is the whole safety property of per-cluster deployment); a read
  that fails **and** a successful read returning nothing both refuse to
  write, because either one reaching the reconcile would free everything
  the cluster holds; and `IngestService` carries the whole `openshift`
  object forward untouched, so a vendor collector can never blank it.
  `OpenShiftState` has **no "nobody looked yet"** — `AVAILABLE` is the
  default and the only state reached by absence. `OpenShiftLifecycle` is
  five fields on purpose (`cluster_id`, `role`, `node_name`, `agent_id`,
  `bmh_name`, `boot_mac` were all removed 2026-09-10); the ADR says why
  each went.
- **These jobs are a *separate* Helm chart**, `deploy/helm/openshift-
  membership`, one release per cluster — they run inside every OpenShift
  cluster, not beside the API. `deploy/helm/server-scan` is still
  the platform itself. A kustomize `cronjobs/` tree used to hold this and
  is deleted; don't resurrect it.
- **A change that is correct on a fresh database is not automatically
  correct on an existing one**, and this bit three times on 2026-09-10
  alone: a narrowed enum whose old values no longer decoded, an index
  rename that left the old unique constraint enforcing, and a nullable
  sort field. Every one shipped green because every test ran against a
  database the new code had just created. **Before shipping a stored-shape
  change, write the old shape into a real database and read it back.**
  `docs/adr/0026-nullable-sort-fields.md` has the rule and the two
  mechanisms that now exist for it: `OpenShiftLifecycle`'s
  `mode="before"` validator, and `indexes.RETIRED_INDEXES` — **renaming or
  removing an index means adding its old name to that list**, or a
  deployed database keeps enforcing the old one forever.
- **The UI is dark only, and four things enforce it together** — the dark
  values are the only token values in `index.css` (no
  `prefers-color-scheme` block), `color-scheme: dark` carries the
  browser's own scrollbars and controls, `index.html` sets a
  `color-scheme` meta plus an `html` background so the pre-stylesheet
  frame is not white, and `@custom-variant dark (&)` makes Tailwind's
  `dark:` utilities unconditional. That last one is the easy miss: 49
  `dark:` classes across 13 components are media-query gated by default,
  so forcing the palette without it leaves a dark page wearing light
  badges on a light OS. **The check worth repeating: `grep -c
  prefers-color-scheme frontend/dist/assets/*.css` must be 0.**
- **Sorting on a nullable field needs the null-aware cursor**
  (ADR-0026). Mongo's `$gt`/`$lt` are type-bracketed: `{$gt: null}`
  matches *nothing* and `{$lt: "abc"}` skips every null, while the sort
  itself orders nulls before strings. A naive keyset clause therefore
  drops rows silently — no error, just a short page. `cluster_name` and
  `mce_name` are the two fields this applies to today.
- **Search tokens are word-boundary suffixes, not bare parts**
  (`docs/adr/0025-search-tokens-are-word-boundary-suffixes.md`). That is
  what lets an *anchored* `^` query find `cisco-m6` inside
  `ocp-cisco-m6-bat-yam-...`. Do not "fix" mid-word search by dropping
  the anchor: measured on 52,087 servers, an unanchored regex costs
  ~650ms per facet query against ~1-26ms, flat, because it scans the
  whole multikey index instead of one range. `search_tokens` is written
  by `IngestService`, so a change here reaches existing documents only on
  their next collection.
- **A server's site is parsed from its name**
  (`app.domain.value_objects.site.parse_site_code`), never taken from
  configuration — `ocp4-prod-tlv-infra-01` -> `tlv`. An ambiguous name
  yields `None` rather than a guess. `None` is a real state the UI shows
  as "Unassigned". A code spelled with a separator (`bat-yam`) matches
  consecutive tokens. **Reversed 2026-09-09, at the operator's explicit
  request: matching is now substring-within-a-token, not whole-token
  only** — `ocp4-computezn-01` matches an alias `zn` glued in with no
  separator of its own, and this is deliberately no longer safe from the
  false positive it used to reject (`ocp4-tlvx-01` now really does
  resolve to `tlv`). A multi-token code (`bat-yam`) still can't
  substring-match, since splitting removes its own `-` from every token.
  **Known real collision, not hypothetical:** a code/alias that is a
  substring of `infra` (e.g. `fra`) makes every `-infra-`-named server
  ambiguous (two sites "matched") rather than landing on the intended
  one — avoiding common role words when picking a code/alias is now the
  operator's job. See the module docstring and ADR-0018's dated update.
  **Canonical codes beat aliases, since 2026-09-10** — a second real
  collision: with `znif|prep:Znif` and `five:Site Five` both configured,
  `ocp4-prep-five-compute-01` carries `five` (a real code) and `prep`
  (someone else's alias) at once, and treating both as one pool dropped
  a name that plainly says `five` to Unassigned. `parse` now tries every
  canonical code first and only consults aliases when that finds
  nothing; ambiguity *within* the canonical tier is still final, it does
  not fall through looking for an alias to break the tie.
  **Two aliases at once picks the leftmost, since 2026-09-10** — unlike
  two real codes, which stays final ambiguity: `fn-data-prep-ocp-
  compute-01` with both `fn` and `prep` configured as aliases (no real
  code present) resolves through `fn` (it reads first), not `None`. No
  real code is ever in play in that case to make picking one a guess the
  way it would be for two canonical codes. See ADR-0018's second
  2026-09-10 update.

  **Which sites exist is `INVENTORY_SITES`, not code** (ADR-0018).
  `SiteCode` is gone; the set is a `SiteCatalog` parsed from
  `"nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"` —
  that string is the shipped default, and an estate sets its own. The
  set is still *closed*, just closed at runtime, and it is still the
  server's own name that picks from it. Three things follow. The catalog
  is threaded explicitly (`IngestService(sites=...)`,
  `parse_site_code(name, catalog)`, `default_system_rules(catalog)`) —
  the domain never reads `Settings`. `Server.site_id` is a plain `str`,
  deliberately, so a document written before a site was renamed away
  still loads. And `INVENTORY_SITES` lives in the shared `api-config`
  ConfigMap because the API *and* every collector must agree on it — a
  collector derives each server's site at ingest. **A Cisco server whose name carries no site
  token falls back to its service profile's org DN**
  (`org-root/org_tlv/ls-worker-01` -> `tlv`) — the name is still the
  authority, the org path is only consulted when it says nothing.
  **A site's code may itself be `|`-separated aliases** (added
  2026-09-08 — `"znif|prep:Znif"`), for two naming conventions that mean
  the same physical site: the first token is canonical (`Server.site_id`,
  every URL), the rest are only ever recognized on the way in, never
  produced. See `SiteCatalog.from_spec` and ADR-0018's dated update.
- **`Vendor` is dell/cisco/hp/standalone — there is still no `UNKNOWN`.**
  `STANDALONE` means *a manufacturer this platform does not model*
  (Lenovo, Supermicro, a whitebox) **or one the BMC did not report at
  all** (`ComputerSystem.Manufacturer` absent/null maps to `STANDALONE`
  too, since 2026-08-23 — a deliberate reversal of the original
  fail-the-system design, accepting a real correlation-key risk to keep
  every listed BMC ingested; see ADR-0016's dated update), **not**
  "collected without a manager": a Dell reached at its own BMC is still
  `dell`, because `IngestService` correlates on `(vendor,
  serial_normalized)` and moving a machine between vendors splits it
  into two documents. Which collector found a server is
  `Server.source_provider`, which is filterable.
- **A provider reports `None` for a field it could not read**, which is
  not the same as zero or empty. `IngestService` carries the stored value
  forward for a `None` and overwrites for a real value. Before this
  existed, a sub-resource that 404'd wrote zeros over good data — which
  took a server from CRITICAL to HEALTHY by reporting no drives, and
  logged an audit event saying the drive had recovered.
- **`Server.unread_fields` says which fields that was**, and directly
  extends the rule above. It is a list of dotted API paths into the
  server's own response (`hardware.gpus`, `hardware.storage.drives`,
  `hardware.power.psus`, `identity.nic_macs`, …) that the *most recent*
  collection could not read, built by `IngestService._carry_forward`,
  returned by `GET /api/v1/servers/{id}`. Two properties are load-bearing:
  it is **recomputed from scratch every ingest and never merged** (a path
  whose value is no longer `None` would otherwise stay flagged forever),
  and **"never successfully read" is deliberately not expressible** — the
  question it answers is about this run. It exists because carrying
  forward is not enough on a *first* ingest: `Hardware` has no "unknown"
  state, so an iLO-4 server that reported nothing stored `0` drives and
  rendered as a confident, real zero.
- **The fleet gauges are computed from MongoDB on scrape, throttled**
  (ADR-0029). `app.observability.fleet_gauges.FleetGaugeRefresher` runs
  `MongoServerRepository.fleet_snapshot` at most once per
  `INVENTORY_METRICS_FLEET_REFRESH_SECONDS`; scrapes in between serve
  the last values, and a failed query leaves them in place. Three things
  bite: the staleness cutoff must be rendered through Pydantic's JSON
  serializer (a `datetime` compares against nothing — ADR-0006); a
  never-seen server (`last_seen_at` absent) counts as **stale**, because
  BSON sorts missing below any string; and collector liveness is
  `max(last_seen_at)`, never `min` — one dead BMC must not make a healthy
  collector read as silent. A retired collector or cluster's label set is
  **cleared** on each refresh, not left at its last value.
  **`servers_partial` ignores a field unread on every one of a
  collector's servers** — that is the collector never reporting it, not a
  read failure; counting it read 100% everywhere. **`Manager.last_run` is
  written by `record_run` and survives `upsert`**, which is a `$set` of
  config fields, not a replace — do not turn it back into `replace_one`.
  `Health.active_policy_keys` is what `policy_active` aggregates; it is
  set only through `pipeline.health_from_state`.
- **The list cache is invalidated by maintenance writes and by nothing
  else** (ADR-0028). `GET /servers` pages (15s) and `/servers/facets`
  counts (60s) are cache-aside with no write invalidation on the ingest
  path — deliberately, since five CronJobs write continuously. The two
  maintenance endpoints call `_invalidate_list_cache`, because the
  operator is looking at the list they just wrote to: without it a server
  taken *out* of maintenance kept showing under `?maintenance=true` for
  the TTL. **Do not extend this to ingest** — that is the decision the
  short TTL exists to express. Two traps if you touch it: `SCAN MATCH`
  has no brace alternation (`{list,facets}` matches nothing, silently),
  and it must never become `KEYS`, which blocks Redis for the whole scan.
- **UNKNOWN is not a health verdict** (ADR-0027). Every fact in
  `app.domain.services.health.facts` counts only *definite* readings — a
  PSU counts as failed on `DOWN`, never `UNKNOWN`; a drive on `CRITICAL`.
  Network was the one exception and it bit: `network.all_links_down`
  compared links-UP against `network.interface_count`, and UCS reports
  `link_state=UNKNOWN` on ~99.75% of real vNICs, so once Cisco collectors
  started reporting vNICs (2026-09-10) nearly every Cisco server read
  CRITICAL on network. Both link policies now use
  `network.links_known_count` (interfaces whose state is anything but
  UNKNOWN) as the denominator; `network.interface_count` keeps its
  literal meaning and rides along as `reported` evidence. `DISABLED` is
  deliberately still counted — it is a real reading. **If you add a fact,
  it must exclude UNKNOWN**;
  `test_health_defaults_coverage.TestUnknownIsNotAVerdict` fails if it
  does not. The engine is still two-valued on purpose — the ADR says what
  would make three-valued evaluation worth its cost.
- **`Server.reachable`/`unreachable_since` are a coarser, whole-server
  version of the same idea, added 2026-09-10 for `OPENMANAGE` only.**
  `ProviderServer.reachable=False` means "I know this server's identity
  but could not reach it at all this run" — every optional field on it is
  `None`, so `IngestService` carries the last-known hardware forward
  exactly as above; it never blanks a server just because one collection
  attempt failed. `unreachable_since` is set once on the first miss, held
  stable across repeated misses, and cleared on recovery. Unlike other
  per-host failures, this one does not count toward `collection_errors`
  (see `docs/dell-collectors.md`'s "Collection flow"), so a fleet with a
  few dead BMCs no longer fails its CronJob pod over them.
- **GPU VRAM comes from a built-in catalog wherever the vendor API does
  not report it — which is everywhere except Redfish.** Corrected
  2026-09-05: this entry used to say flatly that no management plane
  reports a GPU's memory size, which would tell you not to bother reading
  one. Per collector, as the code actually stands:

  | Collector | Real VRAM from the API? |
  |---|---|
  | `REDFISH_STANDALONE` | **yes, attempted** |
  | `OPENMANAGE` (Dell) | **yes** — hardware comes from iDRAC over the same Redfish mapping |
  | `ONEVIEW` | no — `memory_bytes` is hardcoded `None` |
  | `INTERSIGHT` | no — hardcoded `None` |
  | `UCS_CENTRAL` / Manager | no — no such field exists |

  `redfish.mapping.gpus_from_processors` reads
  `MemorySummary.TotalMemorySizeMiB` off a `ProcessorType == "GPU"`
  member (standard since Redfish 1.0), falling back to summing
  `ProcessorMemory[].CapacityMiB` for pre-2020.4 firmware that has no
  `MemorySummary`. **What is unverified is whether Dell or HPE actually
  populate it for arbitrary add-in GPUs** — the path is standard, the
  data is best-effort, and no live hardware has settled it. See
  `docs/field-test-checklist.md` part 3.

  So the catalog is a *fallback*, not a replacement:
  `GpuCatalog.enrich` returns the GPU untouched when `memory_bytes` is
  already set, so a real reading always wins. It exists because Cisco and
  HPE have no field to read at all.
  `app.domain.value_objects.gpu_catalog` ships a table of 30 NVIDIA and
  AMD datacenter cards (`gpu_models.DEFAULT_GPU_MODELS`) and
  `IngestService` enriches from it.
  **`INVENTORY_GPU_MODELS` overrides that table per identifier; it is no
  longer the only source, and an empty value no longer means "enrich
  nothing"** — that reversal is `docs/adr/0021-built-in-gpu-catalog-with-
  model-matching.md`, which supersedes the "deliberately not a hardcoded
  table" reasoning in `docs/cisco-collectors.md`. Three rules matter if
  you touch it: a card is matched on a **Cisco PID *or* a normalized
  model string** (`NVIDIA A100-PCIE-40GB`), because no Redfish or OneView
  GPU reports a PID at all; **the comparison is equality on that
  normalized key, never a substring or fuzzy match** (`A10` vs `A100` is
  one character and 3x the VRAM); and a model that shipped in two
  capacities (`A100`, `V100`, `H100`, `P100`) has **no bare-name row**, so
  it matches nothing and keeps `memory_bytes: None` rather than guessing.
  A value a provider actually read is never overridden. Only add a row
  whose VRAM you can cite from a vendor datasheet or a Cisco spec sheet.
- **Every collector reports power supplies now**, so the health engine's
  `power.psu_count`/`power.failed_psu_count` metrics finally have
  something to read (they had nothing until 2026-09; a server with a dead
  PSU reported HEALTHY on power exactly like one with two good ones).
  Intersight and UCS Manager/Central cover rack units only — a blade's
  supplies belong to its shared chassis, not to the blade — while
  `..redfish.mapping.psus_from_supplies` covers Dell and every standalone
  BMC, and OneView covers HPE with the richest data of the four. Two rules
  are shared by all of them: **an `Absent` supply is dropped, never
  counted as failed** (a four-bay chassis with two fitted is not two
  failed PSUs), and **a PSU's `health` is `UP`/`DOWN`/`DISABLED`/
  `UNKNOWN`, never a `HealthSeverity`** — a policy against
  `power.failed_psu_count` compares to `"DOWN"`, not `"FAILED"`. Redfish's
  `Warning` maps to `UNKNOWN` rather than `DOWN` on purpose: a degraded
  supply still delivering power has not lost redundancy.
  **Corrected 2026-09-07: this rule was itself false for OneView from
  2026-09-01 until that date** — its PSU mapping reported `HealthSeverity`
  values instead, so `power.failed_psu_count` silently counted zero
  failed PSUs for every HPE server the whole time. Found on a live run
  and fixed the same day; see ADR-0022's "Results, 2026-09-07" and
  `docs/hpe-collectors.md`'s "Power supplies" for why this is the third
  time this exact confusion has shipped here.
- **HPE's traps, all of which cost real research** — full detail in
  `docs/hpe-collectors.md`, the decisions in ADR-0022:
  - **The name comes from the server profile.** `server-hardware.name` is
    the enclosure-and-bay location and `serverName` is an OS hostname via
    HPE's Agentless Management Service — both decoys, the same trap
    ADR-0009 records for UCS blades named after their chassis slot.
    Hardware with no assigned profile is **skipped**, counted and logged.
  - **`processorCoreCount` is per processor**, so `cpu_cores` is
    `processorCount * processorCoreCount`. Unmultiplied, every two-socket
    server is half its real core count, silently.
  - **`memoryMb` is MiB**, and HPE says so inline — no assumption, unlike
    Intersight's `TotalMemory`.
  - **`count=-1` means 64, not "all"** on `/rest/server-profiles`, with a
    256 ceiling and truncation HPE documents without saying whether
    paging passes it. An explicit `count` is always sent, and a short read
    logs `oneview.collection_truncated` at ERROR.
  - **`InsufficientFirmware` is "could not read", not zero.** Every
    subresource on an iLO-4 server fails that way; only
    `collectionState == "Collected"` yields data, and everything else —
    `CollectedStale` included — maps to `None`.
- **A collector only ingests servers whose name matches
  `INVENTORY_COLLECTOR_NAME_PATTERN`** (`^ocp` in `.env.example` and
  `values.yaml`; empty = collect everything). A vendor manager holds the
  whole datacenter, and the name is the only thing distinguishing this
  platform's fleet. Applied as a `_NameFilteredProvider` wrapper in
  `tools/run_collector.py`, not inside `IngestService` — collection scope
  is the collector's concern, the seeder shouldn't inherit it, and
  `--dry-run` bypasses `IngestService` on purpose so a filter there would
  make dry runs lie. A non-matching server is never fetched: no document,
  no health state, no audit trail. This is **not** the UPI-vs-hosted
  distinction — that's classification rules over what *is* collected.
  **`REDFISH_STANDALONE` is exempt from the *global***
  (`_UNFILTERED_TYPES`): a BMC does not know the server's `ocp4-...` name,
  so the pattern would discard every host the operator listed. Its
  inventory file is the filter instead.

  **Each manager type can override the global**, keyed on `ManagerType`
  to match how CronJobs, credentials and `PROVIDER_FACTORIES` are already
  partitioned: `INVENTORY_UCS_CENTRAL_NAME_PATTERN`,
  `INVENTORY_INTERSIGHT_NAME_PATTERN`, `INVENTORY_OME_NAME_PATTERN`,
  `INVENTORY_ONEVIEW_NAME_PATTERN`, `INVENTORY_REDFISH_NAME_PATTERN`.
  Unset inherits the global; **explicitly empty is the only way a
  collector opts out of a non-empty global**, which is why the settings
  are `str | None` and why the Helm values render the env var only when
  the key is present (an empty string there would mean "collect
  everything", not "inherit"). **An explicit override also beats the
  `REDFISH_STANDALONE` exemption** — the exemption suppresses the global,
  not an operator who named that collector.

  All of this is reconciled in exactly one function,
  `tools.run_collector.resolve_name_pattern`, and every reader goes
  through it. That matters more than it looks: three collectors prune on
  the pattern *before* `_NameFilteredProvider` sees anything — OME skips
  BMCs, UCS Central skips domains, OneView skips its per-server
  `/powerSupplies` and `/processors` calls — so a factory reading
  `Settings` for itself would let a run prune on the global and filter on
  the override, silently collecting the intersection with nothing logged.
  The resolved value is threaded into the factories instead.
- **A collector's whole connection config is env** — one endpoint and
  login per `ManagerType`. No `Manager` document is read to decide where
  to connect and there is no credentials directory; see the collector
  architecture section above.
- A UCS server's name comes from its **service profile**, not
  `computeBlade.name`, which is empty in practice. Getting this wrong
  names every server after its chassis slot, which carries neither a
  site token nor a classifiable pattern.
- **Cisco collectors (`UCS_CENTRAL`, `INTERSIGHT`) now populate
  `ProviderServer.nics`, not just the flat `nic_macs` list (2026-09-10).**
  Previously only Redfish-sourced collectors did — a UCS/Intersight
  server's `network.interfaces` was always empty. Each vNIC carries no
  FQDD, so `location`/`speed_mbps` stay `None`; `link_state` reads
  `UNKNOWN` on ~99.75% of real vNICs, same as the fabric-attachment
  `oper_state` field. **`cisco_eno_names`
  (`app.domain.value_objects.nic_names`) names them `eno5`, `eno6`, …
  positionally** — a fixed, operator-confirmed rule (`ip link` on real
  hardware), unlike Dell's per-model-configured `NicNameCatalog`. See
  `docs/cisco-collectors.md`'s "The per-interface view" section.
- Every repository stores `datetime` fields as ISO 8601 **strings**
  (`model_dump(mode="json")`), never native BSON dates. Any range/cursor
  query must compare against that stored string type, not a parsed
  `datetime` — this caused a real, silent-wrong-results bug once
  (`docs/adr/0006`).
- MongoDB is the sole source of truth; Redis is cache-aside only and
  every read path degrades to Mongo on any Redis failure — never make
  Redis a hard dependency for correctness.
- Pagination is keyset (HMAC-signed cursor for `/servers`), never
  `skip`/`offset`.
- Sites from configuration rather than an enum is `docs/adr/0018`,
  which supersedes part of `0011`.
- Sites/vendors as closed sets, name-derived sites and the UI rebuild are
  `docs/adr/0011`; env-based manager connections and the single manifest
  set are `docs/adr/0012`; CI action pinning, the removed Dependabot and
  the manual-maintenance obligation are `docs/adr/0013`.
- Health-policy override/shadowing (`policy_key` families) is the
  platform's headline design decision — read `docs/adr/0005` before
  touching anything in `app.domain.services.health`.
- `ucsmsdk` (and any future vendor SDK) is very likely synchronous —
  wrap blocking calls in `asyncio.to_thread`, never call them directly
  from an async context (`app.infrastructure.providers.ucs_manager.
  client`).
- **`ucsmsdk` 0.9.18 emits ~32 `SyntaxWarning`s and they are filtered, not
  fixed.** Its version regexes are non-raw strings (`"\."`), and the pin
  is to what the air-gapped mirror carries, so upgrading is not on the
  table. The warning is emitted at *compile* time, so it appears once per
  fresh venv (every CI run) and once per pod (nothing caches bytecode:
  `--no-compile` at install plus `PYTHONDONTWRITEBYTECODE=1`). Measured
  cost of that recompile: 0.36s cold against 0.24s warm, which is why the
  fix is a filter rather than tens of MB of precompiled bytecode in an
  air-gapped image. Two filters, both scoped to the one message so any
  other `SyntaxWarning` still surfaces: `filterwarnings` in
  `[tool.pytest.ini_options]`, and `PYTHONWARNINGS` in the
  `Containerfile`. **`W605` was added to the ruff gate at the same time
  and is what keeps this safe** — ruff does not select it by default, so
  before that our own invalid escapes were caught by neither the linter
  nor (once filtered) the runtime.
- `requirements.txt`/`pylock.toml` at the repo root are generated
  exports for air-gapped mirroring — regenerate both after any
  `pyproject.toml` dependency change:
  `uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt`
  and the `pylock.toml` equivalent (see `docs/air-gap.md`).
- Frontend E2E (`frontend/e2e/`, Playwright): **if you ever add a page
  with sibling `<select>` fields, do not reach for `getByLabel`.** A real
  Chromium quirk makes a `<label>`'s computed name include every nested
  `<option>`'s text, so "Source" resolves to
  `"SourceSITE_CUSTOMMANAGER_CUSTOMVENDOR_CUSTOM…"` and collides with the
  Vendor field. `docs/adr/0008` has the confirmed behaviour and the XPath
  workaround. The `labeledField` helper that implemented it is gone,
  along with `frontend/e2e/helpers.ts` — the only pages needing it were
  the classification-rule and health-policy editors, which were removed
  when those became read-only. Nothing in the suite has a `<select>` any
  more; the fact is kept here because the next form page will hit it.

## Verifying your work

```bash
scripts/dev-up.sh up                              # Mongo + Redis (podman/docker)
uv sync --all-groups && cp .env.example .env       # first time only
uv run python -m tools.seed_inventory --count 1000 --seed 42

uv run pytest -q                                   # backend: unit + integration + api
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools tests
uv run python scripts/check_comment_density.py    # CLAUDE.md convention 8

cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                                    # needs backend + frontend dev server running
```

Then the step no command covers: **re-read the docs your change made
wrong** (convention 11). `README.md`, `docs/architecture.md`,
`docs/arc42.md` — §9 is the ADR index — `deploy/README.md`, this file and
`.env.example`.

For a real test of the UCS Manager data path (which the Cisco collector
drives per domain) without production hardware:
Cisco's UCS Platform Emulator (UCSPE) is a free, downloadable VM (Cisco.com
login only, no support contract) that runs the actual UCS Manager binary
against simulated hardware and answers real XML API calls — see
`docs/adr/0009` for what's confirmed vs. still assumed about the mapping,
and validate against UCSPE (or real hardware) before trusting this in
production.

**If the suite looks stuck, run `podman ps` before anything else.** The
stack is either not started or has been reaped after a timed-out command
(see below); `scripts/dev-up.sh up` fixes both. Do not go looking for a
regression, and do not cycle `down`/`up` — there is nothing stale to
clear.

This paragraph used to say something else, and a session that acts on the
old version wastes its time, so the correction is worth reading once.

*What it claimed:* rootless Podman containers get reaped between separate
shell commands for want of `systemd` linger, so a stack reported as
started may be gone by the next command.

*What was measured, 2026-09-05:* **the containers really do vanish, but
not between ordinary commands, and not for the stated reason.** This
paragraph was itself corrected the same day — the first measurement was
too short and concluded "did not reproduce", which is why the observation
is written out in full below rather than summarised.

The `linger` framing cannot have been right here whatever else is true:
this environment has no `systemd` at all. PID 1 is `init(Ubuntu)`,
`loginctl` answers "System has not been booted with systemd as init
system", and Podman falls back to `--cgroup-manager cgroupfs`. There is
no linger to be missing, and `conmon` — which is what actually holds a
container open — needs none.

What was observed across one long session, in order:

| Event | Stack afterwards |
|---|---|
| Pod started, `podman ps` in a *separate* later shell call | **Up**, all three `conmon` alive |
| Several test runs completing normally (~38s each) | **Up** |
| `docker compose` stack, several commands | **Up** |
| A `pytest` run that exceeded the tool timeout and was moved to the background | **gone** |
| Restart, run again normally | **Up** |
| A second `pytest` run that exceeded the tool timeout | **gone** |

Two disappearances, both immediately after a command was killed or
detached for exceeding its timeout; no disappearance after any command
that ran to completion, including long ones. That correlation is the
useful part and is what a future session should act on.

**The likeliest mechanism, and it is a hypothesis, not a measurement:** a
timed-out command's process group is cleaned up, and rootless `conmon`
processes started earlier from the same session go with it. The competing
explanation from the original paragraph — Podman's runroot lives under
`/mnt/wslg/runtime-dir/containers`, which WSL can recreate underneath it
— is still possible and still unproven, but it does not explain why the
two failures both landed on a timed-out command and none landed anywhere
else.

**What to do about it:** nothing preventative. Run `podman ps` before
concluding anything about a failing integration run, and after any
command that timed out. Bringing the stack back up is cheap; diagnosing
this is not.

*What the originally reported symptom was:* a separate problem, and not
the vanishing above — a suite that appeared to hang when the stack had
simply never been started. `tests/integration/conftest.py`'s fixtures are
function-scoped, so with the stack down every one of the ~60 Mongo-backed
tests paid `mongo_server_selection_timeout_ms` (5s) over again to
rediscover the same dead server — one file of 7 skips took 35s, the
64-test directory about five minutes. A suite doing nothing for five
minutes reads as hung.

*Fixed*, so this cannot recur: those fixtures now remember the first
unreachable service for the rest of the session and pay the timeout once.
With the stack down `tests/integration` reports `60 skipped in 5.22s`;
with it up, `64 passed in 1.83s`. **A slow integration run is therefore
no longer a symptom of the timeout, and a *stuck* one is not a hang in
the tests — check `podman ps` first.** The two are now easy to tell
apart: a missing stack yields 60 fast skips, not a wait.

Real CI (GitHub Actions) has neither problem; it gets fresh, real service
containers per run.

### Which compose

Three ways to start the dev stack work, one looks like it should and
does not. All measured 2026-09-05 on the user's machine (Podman 6.1.1,
Docker 29.8.0, Docker Compose v5.5.1, podman-compose 1.6.0), each
followed by the integration suite:

| Command | Result |
|---|---|
| `docker compose up -d mongo redis` | works — 64 passed in 2.48s |
| `podman-compose up -d mongo redis` | works — 64 passed in 2.62s |
| `podman compose up -d mongo redis` | **fails** |
| `scripts/dev-up.sh up` | works, no compose provider needed |

**`podman compose` (space) and `podman-compose` (hyphen) are different
programs.** The hyphenated one is the Python implementation and shells
out to `podman run`, so it just works. The space-separated Podman 6
subcommand implements nothing itself — it delegates to the Docker Compose
plugin pointed at a Podman socket, which is normally started by systemd
socket activation. There is no systemd here, so it fails with:

```
failed to connect to the docker API at unix:///mnt/wslg/runtime-dir/podman/podman.sock
```

That is a real, reproducible consequence of the systemd-less environment
— unlike the container-reaping story above, which is not.

**Prefer `docker compose`** for the dev stack, and do not read that as a
verdict on Podman: the `Containerfile` is UBI-based and deploys to
OpenShift, so `podman build` remains the right way to test the image.
This is only about the three dev containers. `scripts/dev-up.sh` stays
the fallback — it depends on no compose provider at all, which is what
the air-gapped and CI paths may need.

**The three paths cannot see each other.** They name containers
differently — `server-scan-dev-mongo` (dev-up.sh),
`server_scan-mongo-1` (docker compose), `server_scan_mongo_1`
(podman-compose) — while all binding 27017 and 6379. So a stack started
one way is invisible to another way's `ps` and still takes the ports.
On a port-in-use error, check all three before concluding nothing is
running.

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

**Most recent, 2026-09-13, later** — **the repository is
`team-redbull/server-scan`**, renamed from `server_scan`. Every `v*` tag
and GitHub Release up to v17.4.3 was deleted at the operator's direction
and versioning restarted: the first release under the new name is
**v1.0.0**, and the images are `ghcr.io/team-redbull/server-scan-api` /
`-frontend` (the old `server_scan-*` packages are deleted from GHCR).
ADR-0010's dated update records it. Two things came out of the same
afternoon: the publish job is now re-runnable after a failed step — a
GitHub API outage left a tag with no release and no images, and the
"version moves forward" guard then refused the re-run until the tag was
deleted by hand — and redbull-platform was pinned to `1.0.0` and
deployed (Synced/Healthy). The operator also stated the real estate:
**~5,000 servers today, up to 10,000** — the scale statements in this
file, `README.md`, `docs/architecture.md` and `docs/arc42.md` now say
that, with the 50k verification kept as the measured headroom.

**Before that, 2026-09-13** — the Claude Code setup itself: `/gate`,
`/docs-sweep`, three enforcing hooks and two review agents, all under
`.claude/` (tracked, per convention 4); MCP servers stay user-level. See convention 7 for what each does. Nothing in the
platform changed.

**Before that, 2026-09-12, later the same day** — staleness
detection (ADR-0029), item 0 of the not-done list: fleet gauges on
`/metrics`, a `ServiceMonitor` + `PrometheusRule` in the chart, and the
frontend's nginx collapsed to three `location` blocks. Also the same day:
maintenance is switched only from the inventory list now (the detail page
is read-only for it), the Name column is left-aligned with everything
else centred, and the Helm chart's fake collector seeds 2,500 servers to
match the operator's real estate.

**Earlier the same day** — eight operator-requested changes, all
shipped. The deployment ones first:

- **The Helm chart is `deploy/helm/server-scan`**, renamed from
  `server-inventory`, along with `app.kubernetes.io/part-of`, the
  `serverScan.*` template helpers, the project name in `pyproject.toml`,
  `INVENTORY_SERVICE_NAME`, `scripts/dev-up.sh`'s pod name, and **the
  Mongo database and user, both now `server-scan`**. The database rename
  was made at the operator's explicit direction after the orphaning risk
  was raised: **an existing deployment's data stays in the old
  `server_inventory` database and must be moved by hand** — `mongodump
  --db server_inventory` then `mongorestore --nsFrom 'server_inventory.*'
  --nsTo 'server-scan.*'`. A hyphen in a Mongo database name is legal and
  was verified against a real server, not assumed; only the `mongosh`
  shell needs `db.getSiblingDB("server-scan")` rather than dotted access,
  since `db.server-scan` parses as subtraction.
- **Exactly one Route, and it does not gain one per endpoint.** The Route
  only gets traffic into the cluster; the frontend's nginx decides which
  paths belong to the API and forwards them to its Service
  (`frontend-api-proxy-configmap.yaml`, mounted at
  `/etc/nginx/api-proxy.d`, which `frontend/nginx.conf` includes). So
  `route.apiPaths` and the five path-scoped API Routes are gone, and
  **exposing another API path is one `location` block there** — no second
  Route, no second hostname. Currently forwarded: `/api/`, `/health/`,
  `/metrics`, `/docs`, `/redoc`, `/openapi.json`. It is a list rather
  than a catch-all because the SPA owns `/` and has its own client-side
  routes (`/servers`, `/rules`, `/health-policies`) that must not be
  proxied.
- **The standalone Redfish TOMLs live in the chart**, at
  `deploy/helm/server-scan/files/redfish/`, read with `.Files.Get`
  (`collectors.redfishStandalone.inventoryFile`/`credentialsFile`). The
  inventory file is the default source; **the credentials file is opt-in
  and empty by default because rendering it puts BMC passwords in git**,
  and `credentialsSecret` still wins.
- **`helm template` passing proves nothing about a missing value.** Helm
  renders an absent `.Values.x` as an empty string with no warning, so a
  mis-nested values file (a block inserted between a map's `enabled` and
  its other keys — done twice on 2026-09-13, in this repo's own
  `values.yaml` and again in redbull-platform's copy) rendered
  `expr: server_scan:collector_silent_seconds >` and sailed through
  `helm lint`, `helm template` and `promtool`. Only OpenShift's
  `prometheusrules.openshift.io` admission webhook rejected it, at Argo
  sync time. Two things now stand in the way: every threshold the
  PrometheusRule reads is wrapped in `required`, so the render fails
  with a message naming the key; and **before pushing a chart change,
  run it against the real cluster** — `helm template ... | oc apply
  --dry-run=server -f -` exercises every admission webhook, which nothing
  offline can.
- **CI has a `helm` job** that lints every chart under `deploy/helm`
  (discovered, not listed) and `helm template`s each one under the value
  combinations the defaults never reach. It uses the runner's
  preinstalled helm rather than `azure/setup-helm`, so ADR-0013's
  SHA-pinning obligation gains nothing new to maintain.

And the four application ones: the Unassigned site card is hidden while empty (a configured
site still shows at zero); the Redfish credential circuit breaker is
deleted so every BMC is attempted every run, with `OPENMANAGE` now
writing a `reachable=False` placeholder for a rejected login as well as a
dead one; the inventory table gained a one-click per-row maintenance
switch; and UNKNOWN stopped counting as a health verdict (ADR-0027 —
this was a real fleet-wide false CRITICAL on Cisco, not a hypothetical).
See the "Key technical facts" entries for the last two.

**Work before that, 2026-09-10** — cluster membership, finished and
documented. `Server.openshift` is now written by two real CronJobs
(`docs/adr/0024-openshift-cluster-membership.md`), `OpenShiftLifecycle`
was trimmed from ten fields to five at the user's direction,
`OpenShiftState` narrowed to AVAILABLE / INSTALLED /
INSTALLED_TO_INVENTORY, and the kustomize tree at `cronjobs/` was replaced
by a Helm chart at `deploy/helm/openshift-membership` (one release per
cluster, for ArgoCD). Three UI changes landed with it: search now finds
mid-name fragments (`docs/adr/0025-...`), the sites landing page gained
Available/Installed cards, and `?site_id=unassigned` works — it never had,
so the site overview's own Unassigned card had always linked to an empty
list.

That commit also fixed the five CI failures the previous one shipped red,
plus a real runtime bug `ty` caught only because CI never got that far:
`AuditService(...)` called positionally against a keyword-only parameter,
which would have raised `TypeError` on every real collector run.

**Convention 8 is now a CI gate**, not the honor system — see the
convention itself. `scripts/check_comment_density.py` with a baseline of
701 pre-existing violations that may only shrink.

The most recent user direction before that was: real vendor collectors
first, deployment/CD gaps and auth deliberately parked. **Every planned vendor
collector now exists, and as of 2026-09-08 every one of them has had a
live field pass against real hardware, every one finding at least one
real defect** — `UCS_MANAGER`/`UCS_CENTRAL` against UCSPE, `ONEVIEW`
against a live appliance (ADR-0022's "Results, 2026-09-07"), `INTERSIGHT`
against the user's on-prem Private Virtual Appliance, both
`verify_intersight` and `--dry-run` itself (ADR-0017's "second field pass"
section — auth, name resolution, `TotalMemory`'s MiB unit, and five real
defects found and fixed: the `ComputeBoard`-only join gap, a GPU catalog
matcher that couldn't recognize Intersight's own product-name spelling,
and `"OK"` reading UNKNOWN instead of UP/HEALTHY across PSU health,
GPU/NIC `oper_state`, and drive health), and, last to close, `OPENMANAGE`
against a live OME appliance and several iDRAC9 servers on 2026-09-08
(ADR-0020's own flagged "highest-consequence unverified assumption" —
that iDRAC's `SerialNumber` is the Service Tag OME correlates on — turned
out wrong; the real one is `Oem.Dell.DellSystem.NodeID`, fixed the same
day). The natural next steps:

1. **UCS's own leftovers — settled 2026-09-07 by a live UCS Central dry
   run**, see ADR-0009's two "Update (2026-09-07)" sections. **Settled:**
   `total_memory`'s MB assumption is correct (confirmed against the UCS
   UI's own figure, and this also backs Intersight's identical
   assumption); `cpu_model` and per-drive storage detail are confirmed
   populated on real hardware, not just present in the mapping code;
   fabric `fabric_model`/`fabric_serial` are confirmed populated too
   (this was already implemented, just never recorded in the ADR until
   now); and the `health`/`oper=UNKNOWN` question is fully settled —
   `_DISK_HEALTH_MAP` was missing two real failure states (`offline`,
   `self-test-failed`, both now CRITICAL) and `_OPER_STATE_MAP` was
   missing five real `AdaptorExtEthIf` values, both closed against the
   installed `ucsmsdk`'s authoritative enums rather than only what this
   fleet happened to show. Three more raw values were confirmed to be
   correct as UNKNOWN, not gaps: disk `NA`/`unknown` genuinely mean
   "doesn't apply"/"no verdict" in Cisco's own terms, and interface
   `indeterminate` (24% of this fleet's physical ports — common) is
   Cisco's own name for "cannot be determined". A fourth finding wasn't a
   bug at all: `AdaptorHostEthIf.oper_state` (vNICs) turned out to be a
   generic equipment-operability enum, not a link-state one, so reading
   `"unknown"` on 99.75% of vNICs is expected given what the field
   actually measures — no fix exists to make there. **`fabric_name` is
   now built and confirmed live** — `topSystem.name` (the domain's shared
   cluster name; UCS Manager has no per-FI hostname), previewed first in
   `verify_ucs_central`'s section 6, then independently confirmed by the
   user running a short `ucsmsdk` script directly against a real
   air-gapped domain before it was wired in. One more domain-singleton
   query per domain (`ucs_manager/provider.py`), threaded through
   `_attachments`'s new `cluster_name` param. See ADR-0009's second
   "Update (2026-09-07)". **Still open:** `fabric_id` (no source exists)
   and a fully *associated* service profile (nothing tested has gone past
   `config-failure` for want of a boot policy, vNICs and a UUID pool).

2. **OpenManage's own remaining narrow item**: `Oem.Dell.DellSystem.
   NodeID` is confirmed only on iDRAC9. Worth a quick check on any iDRAC7/8
   hardware this estate still runs — those generations may not carry the
   OEM block the same way, and `mapping._dell_serial` falling through to
   `SerialNumber` there would silently reintroduce the wrong serial for
   that generation only.

3. **The Dell iDRAC GPU VRAM check** (`docs/field-test-checklist.md`
   part 3) — one `curl`, opportunistic, only if a Dell server with a GPU
   fitted is ever to hand. Settles whether the built-in GPU catalog needs
   to carry Dell's own spellings or Redfish's `MemorySummary` already
   covers it.

4. **Intersight's own remaining narrow items**, none blocking: the
   DOWN/CRITICAL counterpart to Intersight's `"OK"` vocabulary
   (`normalize_oper_state` and `_drive_health` both), unconfirmed because
   nothing on the tested tenant has actually failed; boot-optimized
   storage (`FlexUtil`/`FlexFlash`), confirmed real but not implemented;
   and the smaller ADR-0017 UNVERIFIED-list items (CPU-name field, BMC
   address precedence, clock skew, account region). Worth another
   `verify_intersight` pass opportunistically, not a scheduled action.

5. **Then the deployment/CD and auth gaps above**, which are the rest of
   what "production and really run" means for this platform — staleness
   detection first, since it is item 0 of the not-done list and nothing
   else answers "40 hosts have been failing for two weeks". Ask the user
   before assuming this is the next phase; the ordering above is the
   direction they have been steering toward, not a plan they have signed
   off on.

~~Give the Dell collector a seeded shape~~ — **already done, kept here as
a standing caution rather than deleted.** `_UNSEEDED_COLLECTORS` is
empty, `COLLECTOR_TYPES` shapes all five collectors including
`OPENMANAGE`, and `provider_type_for` discriminates Dell by
`server.vendor` rather than by `external_id` prefix. Verified 2026-09-07
(Phase 11 of `docs/notes/2026-09-refactor-plan.md`) — this exact item was
stale once before a session caught it, so double-check before trusting it
a third time.
