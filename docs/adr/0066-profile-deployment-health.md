# ADR-0066: A failed OME profile deployment makes a Dell server CRITICAL

**Status:** Accepted, implemented 2026-11-19.

## Context

A Dell server managed through OpenManage Enterprise (OME) can have a server profile whose last deployment
run failed, so its configuration never completed. Nothing in the inventory showed it. The fact is already in
the `/ProfileService/Profiles` entry the `OPENMANAGE` collector reads in bulk: `LastRunStatus`, an integer
that uses OME's job-status ids. No Dell page we could fetch tabulates them (the API guide PDF refuses
automated fetching), so the table was read from the operator's appliance (`GET /api/JobService/JobStatuses`,
2026-11-19): 2020 Scheduled, 2030 Queued, 2040 Starting, 2050 Running, **2060 Completed**, **2070 Failed**,
2080 New, 2090 Warning, 2100 Aborted, 2101 Paused, 2102 Stopped, 2103 Canceled, 2200 Not Run.

The estate that day (884 profiles): 877 Completed, 3 Failed, 4 not attached to any server (`TargetId` 0,
`TargetName` empty; two Completed, two Not Run). `ProfileState` is undocumented (4 on every attached profile,
0 on the four detached ones) and is not read.

## Decision

1. **`OmeIdentity` carries `LastRunStatus`** (`last_run_status`, `last_run_status_name`) beside the template
   fields; `ProviderServer.profile_last_run_status` and `_name` carry it into ingest, including onto the
   `reachable=False` stubs (OME was read even when the iDRAC was not). An id missing from the table keeps its
   number (`Status 9999`); a missing or non-integer `LastRunStatus` is `None`.
2. **Stored as `Server.profile_deployment`** (`last_run_status`, `last_run_status_name`), additive with a
   default, so documents written before it load unchanged. Only `OPENMANAGE` writes it; for any other collector
   ingest leaves it alone and never lists it in `unread_fields`. `None` from OPENMANAGE means "could not read":
   the stored value is kept and `profile_deployment.last_run_status` is listed as unread (ADR-0016).
3. **A new health category `profile`** with the system policy `profile.deployment_failed` (CRITICAL, scope
   `OPENMANAGE`): fires on 2070. **Only a finished run is a verdict** (`profile.has_data` is true for 2060 and
   2070): Completed is HEALTHY; Not Run, Scheduled, Queued, Starting, Running, New, Warning, Aborted, Paused,
   Stopped, Canceled and an unread value are UNKNOWN, never a free HEALTHY and never CRITICAL (ADR-0027). A
   failed profile therefore makes the server CRITICAL overall.
4. **The API and UI show it**: `health.profile` everywhere `health` is, `profile_deployment` on the server
   detail, a Profile row in the health breakdown and a "Profile deployment" field on the Overview, both for
   `OPENMANAGE` servers only.

## Consequences

- Day one on the operator's estate: 3 servers turn CRITICAL, nothing else changes.
- Warning (2090) and the Aborted/Paused/Stopped/Canceled ids are UNKNOWN until one is seen on a real profile and
  the operator decides its severity; none existed on 2026-11-19. Adding one is one more policy plus its id in
  `_PROFILE_JUDGED` (`facts.py`).
- The ids rest on that one appliance. A profile's `LastRunStatus` is assumed to use the job-status table; the
  2060 and 2070 readings on real profiles agree with it.
- The failure's cause is not collected. `DeploymentTaskId` names the job; reading its history is a follow-up.
