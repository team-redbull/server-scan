# ADR-0068: A degraded UCS virtual drive is a finding, and the disk it lost is reported

**Status:** Accepted, implemented 2026-11-22.

## Context

A Cisco rack server (`sys/rack-unit-21`, 2026-11-22) had a two-disk mirror that lost a disk. UCS Manager showed
`vd-1000` as `drive_state=degraded`, `operability=degraded`, `oper_qualifier_reason="Drive state degraded"`,
`presence=mismatch`, and listed `disk-2` as `presence=missing`, `disk_state=unknown`, empty model, serial and size
(`disk-1` was `equipped` and `online`). The inventory showed the server HEALTHY with one drive:

- `_storage_drives` kept only `is_equipped()` disks, so the missing leg vanished. `is_equipped` is a compute-server
  test (`presence` starts with `equipped`) and was reused for disks.
- `ucs_manager/raid.py` already mapped `drive_state` to `StorageVolume.health` (`degraded`, `partially-degraded`,
  `cache-degraded`, `rebuilding` to WARNING, `offline` to CRITICAL), but no fact read it. ADR-0048 leaves a degraded
  mirror `PRESENT` and hands the legs' health to the storage category; the one bad leg was the one dropped.

UCS reports a bay with nothing in it as `presence=empty` (already dropped, tested) and a disk it expects but cannot
see as `missing`.

## Decision

1. **A `missing` disk is reported as a drive** (`health=CRITICAL`, `health_detail="missing"`, no model, serial or
   capacity, so it adds nothing to the total), **only when a virtual drive on the same controller is not optimal**
   (`raid.degraded_controller_dns`: parent DN equal, drive state mapped to WARNING or CRITICAL, `cache-degraded`
   excluded because a cache or battery fault is not a lost leg). The match is per controller, not per virtual drive: a
   second pulled disk on the same controller (a spare, another volume's member) is reported too, which the MOs cannot
   tell apart. A `missing` disk
   beside an optimal, unknown or absent virtual drive is still dropped, and so is `empty`. The gate is the
   conservative choice: only one live capture exists, and an unconditional rule could raise a CRITICAL for every
   bay a chassis lists as `missing` for another reason.
2. **Three facts, `raid.degraded_volume_count` (volumes reading WARNING), `raid.failed_volume_count` (CRITICAL) and, since 2026-11-25, `raid.missing_drive_count`**,
   count every controller's volumes, whatever the RAID level.
3. **Three system policies in the `raid` category** (`raid.drive_missing`, CRITICAL, is the 2026-11-25 update below), scoped to `UCS_CENTRAL` and `UCS_MANAGER`:
   `raid.volume_degraded` (MAJOR, "N virtual drive(s) degraded", the same tier as one bad OS disk) and
   `raid.volume_failed` (CRITICAL, offline). Dell and HPE volumes already carry a `health` from their collectors;
   their behaviour is deliberately unchanged here (ADR-0030 scoping).
4. **The RAID verdict is untouched**: a degraded mirror stays `PRESENT` (ADR-0048), so `ServerScanServersWithoutRaid`
   and `raid.os_mirror_missing` do not change.

## Consequences

- A server with a degraded virtual drive turns MAJOR (CRITICAL when a leg is missing, see the 2026-11-25 update) on the next UCS collector run: its drives now include the
  missing leg, so the document changed and ingest rewrites and re-evaluates it (a seeded policy is inserted at
  startup). Nothing for the operator to do; `POST /servers/{id}/health/recalculate` shows one at once.
- The missing leg has no capacity, so `facts.py` keeps it (`health_detail == "missing"`) out of the OS and data
  disk buckets: it counts only in `storage.failed_drive_count` and the raid policies (`raid.volume_degraded`, `raid.drive_missing`) judge it. Without that, a
  lost boot-mirror leg raised "Data disk degraded" and, on a `-10tb-` server with one other bad data disk, the
  large-storage CRITICAL tier.
- Not verified: other `presence` values a lost disk may show (`mismatch`, `inaccessible`), a rebuild in progress
  (`disk_state=rebuilding` on the new disk and `drive_state=rebuilding`, mapped to WARNING, so MAJOR until it
  ends), and blade-server controllers. One live capture (rack-unit-21).
- Intersight does not read virtual drives and is not covered.

## Update 2026-11-25: a missing disk is CRITICAL, a degraded one stays MAJOR

The operator saw a server with a lost mirror leg read MAJOR, headlined "1 virtual drive(s) degraded", the same as a
disk that was only degraded. A lost leg is the worse case. The missing drive was already CRITICAL as a drive, but it
sits in no OS or data bucket (above), so no storage policy counted it and the volume policy was the only verdict.

- **New fact `raid.missing_drive_count`** (drives with `health_detail == "missing"`) and **new system policy
  `raid.drive_missing`** (CRITICAL, "N drive(s) missing", `raid` category, `UCS_CENTRAL` and `UCS_MANAGER` only). The
  server turns CRITICAL and the reasons list, worst first, leads with it. `raid.volume_degraded` still fires beside it.
- A disk that is present but `WARNING` (degraded, rebuilding) is unchanged: MAJOR.
- It is in the `raid` category, not `storage`, so a lost leg still never counts as a data disk (the 10 TB tiers).
- Not changed, and open: a present disk whose own health is CRITICAL (failed) on a mirror is still MAJOR under
  `storage.os_disk_bad_major` (one bad OS disk). Whether a failed disk should also be CRITICAL is the operator's call.
- Seeded UCS servers with a degraded mirror (the fake adds the missing leg) now read CRITICAL, not MAJOR. A seeded
  policy is inserted at startup, so nothing to do; `POST /servers/{id}/health/recalculate` shows it at once.

