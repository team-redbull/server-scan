# Fake data

Moved out of `README.md` (2026-10-03). `tools/seed_inventory.py` is the only way to get a fleet without vendor hardware; the figures below are for `--count 1000 --seed 42` and must be re-measured if the generator changes.

`tools/seed_inventory.py` is the only way to get a fleet without vendor
hardware. It runs the *real* ingestion pipeline — the same
`ProviderServer` -> classify -> health-evaluate -> audit -> upsert path a
collector drives — so seeded data exercises what production does rather
than a shortcut that writes documents directly.

```bash
uv run python -m tools.seed_inventory --count 1000 --seed 42
```

`--count` defaults to 1000 and `--seed` to 42; the same pair always
produces the same fleet, field for field.

`--epoch N` (default 0) re-seeds the same fleet with a few percent of
servers' health changed (failed drive, PSU down, links down, degraded DIMM,
failed GPU, or recovered), so a second run produces `HEALTH_CHANGED`
events in the audit trail. `--epoch auto` picks `floor(unix_time / 21600) % 8`.

```bash
uv run python -m tools.seed_inventory --count 1000 --seed 42 --epoch 1
```

What you get mirrors the collectors that exist. Cisco blades arrive
as `source_provider=UCS_CENTRAL` with Central-rooted DNs, service-profile
org paths and fabric attachments; Cisco rack units arrive as
`INTERSIGHT` with `intersight/<moid>` ids and no org path; HPE ProLiants
arrive as `ONEVIEW` with `/rest/server-hardware/<uuid>` ids; Dell servers
arrive as `OPENMANAGE`; and genuinely standalone whiteboxes arrive as
`REDFISH_STANDALONE` with `redfish://` addresses. All five collectors are
seeded and all five are filterable from the UI's Source filter. Each collector's
*absences* are reproduced too, because a fixture richer than the real
thing hides the gaps worth seeing. Names span the estate's real shapes,
including a deliberate minority carrying no site token, so "Unassigned"
is reachable.

**What to look at once it's seeded** (`--count 1000 --seed 42`):

* **The sites overview's six fleet cards** read 1000 across all sites,
  585 UPI, 313 hosted-cluster and 102 MCE on the first row, then 204
  available and 796 installed on the second. Measured, not estimated —
  re-measure rather than trust these if the generator changes. Nothing
  lands in `UNCLASSIFIED`, which is not a seeding gap: UPI's default rule
  became an unconditional `.*` on 2026-09-10, so the system rules cannot
  produce that state at all. Every card is meant to be non-empty and
  visibly different — an available server is a real state, not a seeding
  accident.
* **Contested servers (ADR-0041)**: about 1% of installed servers carry `contested_with`,
  so the inventory's Duplicate Server filter and the gauge have data.
* **The four site cards plus Unassigned**: 211 nyc, 224 tlv, 224 bat-yam,
  220 five, and 121 with no site token in the name. That last one is why
  the Unassigned card is worth having — and why it is hidden when it
  would read zero, since on a real estate it usually does.
* **Both network link policies fire, and only Intersight is exempt.**
  25 servers report every readable link down (CRITICAL) and 20 report one
  up (MAJOR) — `docs/adr/0027-unknown-is-not-a-reading.md`. Before that
  ADR every UCS/Intersight server was all-`UNKNOWN` and read CRITICAL
  regardless, which is what made the real cases invisible; since
  2026-09-14 a UCS Central vNIC's state comes from `operability`, a real
  signal, so 3 of the 25 CRITICAL and 3 of the 20 MAJOR are genuinely
  UCS Central servers now (docs/cisco-collectors.md). Only the 160
  Intersight servers still report `UNKNOWN` for every vNIC and are
  scored on nothing — no equivalent field is known for it yet.
* **34 servers have an unreachable BMC, and all of them read `CRITICAL`.**
  About 4% of the OpenManage (10), standalone Redfish (12) and OneView (12)
  servers carry an `unreachable_reason`, which the `bmc` category's
  `bmc.unreachable` policy turns into CRITICAL (ADR-0037). Nothing is
  `UNKNOWN` overall in this fleet any more: before 2026-10-01 the ten
  OpenManage ones were, since no hardware field was read and a category
  nothing was read for judges nothing
  (`docs/adr/0027-unknown-is-not-a-reading.md`); before 2026-09-21 they
  read HEALTHY.
* **Availability is not derived from the name.** A seeded server named
  `ocp4-prod-tlv-compute-01` can come back `AVAILABLE`, because a freed
  server keeps the name it was installed under. That disagreement between
  what a name claims and what a cluster reports is the whole point of
  keeping the two apart (ADR-0024) — if only `random-server-*` entries
  were ever free, the Available card would be lying about the shape of a
  real fleet.
* **GPU VRAM comes from the catalog, not from the fixture.** Only
  Redfish-sourced collectors can read a GPU's memory; Cisco and HPE have
  no field for it (see
  `docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`), and the
  fake provider models the catalog path: every generated GPU carries
  `memory_bytes=None` and whatever the UI shows was filled in at ingest
  by `GpuCatalog`. Cisco-collected servers report a PID
  (`UCSC-GPU-L40S`), Dell/HPE ones the vendor's model string
  (`NVIDIA A100-PCIE-40GB`, `AMD Instinct MI300X`), and both match. A
  minority deliberately carry a card the built-in table does not
  answer for — a PID it has never been taught (`UCSC-GPU-A100-40`), a
  genuinely ambiguous `NVIDIA A100` (the A100 shipped in 40GB *and*
  80GB), an absent `NVIDIA RTX A6000` — and those show the raw
  identifier with no VRAM. That is the state `INVENTORY_GPU_MODELS`
  exists to close, and it has to be visible here rather than discovered
  in production.
* **HPE spans three ProLiant generations.** Gen11 and Gen10 inventory
  fully; Gen9 carries an iLO 4, against which every OneView subresource
  call fails, so those servers come back as identity-only records — the
  provider reports `None` for drives, GPUs and NICs, never zero or an
  empty list. (The stored document still shows `0`/`[]` on a *first*
  ingest: `Hardware` has no "unknown" state, and the `None` contract's
  job is to stop a later run overwriting good data — see
  `docs/adr/0016-redfish-standalone-collector.md`.)

**Re-seeding needs an empty database.** Servers correlate on
`(vendor, serial)`, so seeding a different `--count`/`--seed` (or a fleet
generated before a change to the generator) over an existing one reports
errors rather than replacing it. Wipe first:

```bash
scripts/dev-up.sh down && scripts/dev-up.sh up
uv run python -m tools.seed_inventory --count 1000 --seed 42
```
