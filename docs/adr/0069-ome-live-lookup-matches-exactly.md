# ADR-0069: The live OME lookup matches exactly and joins on TargetId

**Status:** Accepted, implemented 2026-11-25.

## Context

`OpenManageProvider.get_one()` (ADR-0032) is what `POST /servers/{id}/refresh` (ADR-0064) and the
`POST /servers/available` live recheck call. `_discover_one` asked OME for a profile with
`ProfileService/Profiles?$filter=TargetName eq '<device name>'` and took `profiles[0]`; the three other lookups it
makes (device by service tag, profile by `ProfileName`, device by `DeviceName`) did the same with `[0]`.

The operator found on 2026-11-25 that this is wrong on their OME. A query for `TargetName eq '10.0.0.1'` returned ten
profiles, `10.0.0.1` and `10.0.0.10` to `10.0.0.18`: **`eq` on a name is a prefix match, and the rows come back in no
useful order** (measured live; Dell documents neither, see `docs/notes/2026-11-25-ome-profile-filter-semantics.md` and
`docs/dell-collectors.md`, "`$filter ... eq` on a name is a prefix match"). So refreshing `10.0.0.1` could take the
profile of `10.0.0.11` and combine it with the right device: the iDRAC address, service tag and model of one server with
the name, template, profile id and last-run status of another. The name drives site and classification, and the stored
server is correlated by service tag, so a refresh could rename it.

Also measured: `DeviceServiceTag eq` returns one exact row; `$filter=TargetId eq <id>` is **not supported** (HTTP 501,
`CGEN1006`); and a profile's `TargetId` equals its device's `Id`.

The scheduled collector was never affected: it downloads every profile and device and joins in Python on exact names.
The unit tests missed it because their OME fake ignored `$filter` and returned everything, and no test called
`_discover_one` at all.

## Decision

1. **A filtered OME result is a candidate list, never an answer.** After every `$filter ... eq` lookup in
   `_discover_one`, the rows are reduced to those whose searched field equals the wanted value exactly (`_exact`). This
   is mandatory, not a fallback, and the code never falls back to `rows[0]`.
2. **The profile joins its device by id.** Among the candidates, the profile whose `TargetId` is the device's `Id` is
   used (`_same_id`: both present and non-zero, `0` meaning "no device"). OME cannot filter on `TargetId`, so it is
   checked after the fetch. A profile with a non-zero `TargetId` that is not the device's `Id` belongs to another
   device and is refused, even when its name matches. Only when no candidate names another device (`TargetId` absent or
   `0`) is the single profile whose `TargetName` is exactly the device's name used, with `ome.profile_id_mismatch`
   logged. The id equivalence was confirmed on one server only; a wrong name is worse than a miss.
3. **An ambiguous match is refused, not guessed.** Two exact matches that the id does not separate give `None` and an
   `ome.ambiguous_match` warning, so the caller answers "not at that manager" (409 for a refresh, the next candidate for
   `/servers/available`). A wrong name is worse than a miss. The bulk collector keeps its first-wins rule and its
   duplicate log; this ADR does not change it.
4. **The profile-name fallback** (reached with no service tag, or when the tag gives no device or no profile) filters `ProfileName` and checks `ProfileName` exactly; the device is then
   the one whose `Id` is the profile's `TargetId`, else the single device named exactly the profile's `TargetName`. When
   a device is already known from the tag, a profile found by name that targets a different device (neither `TargetId`
   nor name agree) is rejected with `ome.profile_of_another_device`.

5. **Also refused:** a profile found by name whose only device of that `TargetName` is bound to another profile
   (`ome.profile_of_another_device`). The service tag is compared case-insensitively (OME's case rule there is not
   verified); names are compared exactly. A refused or ambiguous lookup reads as "not found" to the caller (a refresh
   answers `SERVER_NOT_AT_MANAGER`); the `ome.*` warning says which.

## Consequences

- A refresh or recheck of `10.0.0.1` now gets `10.0.0.1`'s profile whatever order and prefix neighbours OME returns.
  Operator: nothing to configure.
- Servers already refreshed under the wrong name: the next OpenManage collector run rewrites the name from the exact
  join (by reading the code, not verified live); the wrong `SERVER_RENAMED` events stay in the audit trail.
- The queries are unchanged, so a lookup returns up to the prefix set (ten rows in the measured estate, all on one page:
  `@odata.count` equalled the rows and there was no `@odata.nextLink`). `get_all` follows `nextLink`, so a longer set is
  still read in full.
- The id equivalence rests on one server; the exact-name fallback covers the rest. A live probe of a few more servers
  would let the fallback be removed.
- The Dell collectors' tests now use a fake OME with prefix matching and the wrong row first
  (`tests/unit/infrastructure/providers/test_openmanage_exact_match.py`).

## Other lookups audited (2026-11-25), not changed here

A read-only sweep of `backend/app`, `tools` and the repositories for the same class (a name, serial or address
matched loosely, then the first result trusted) found the scheduled collectors, the ingest correlation, the Mongo
repositories, the Kubernetes Job/pod ownership checks and `GET /servers/available?name=` exact by construction.
The remaining first-wins spots, each a separate decision because each changes behaviour that cannot be checked
without the vendor:

- **UCS Central `get_one` name fallback** (`ucs_central/provider.py`, `next(...)` over every non-template `lsServer`
  with a case-insensitive name match): two profiles of the same name in different orgs or domains give the first.
  Only reached when the stored DN is missing or no longer resolves.
- **Redfish `get_one`** (`redfish/provider.py`, `next(..., servers[0])`): a serial that matches no system on a
  multi-system BMC returns the first system. The host match is exact, so `10.0.0.1` vs `10.0.0.10` is fine.
- **UCS Manager `get_one` and the Central known-domain path** read the blade at a stored DN and never compare the
  serial, so a swapped blade is ingested as the old server's recheck.
- **Intersight `get_one`** takes the first row of `Serial eq '<serial>'` without re-checking it. Intersight's `eq` is
  normally exact, so this is only a risk if a tenant matches loosely; it needs a live probe like the OME one.
- **By design, documented:** the `pattern` mode of `/servers/available` is an unanchored regex (a caller sending
  `ocp-worker-1` can reserve `ocp-worker-12`); OpenShift membership writes to the lower `_id` of two servers with
  one hostname when no serial can separate them (logged); the audit `server_name` filter and the inventory search
  are prefix or substring searches; name patterns in collector config are unanchored regexes.

