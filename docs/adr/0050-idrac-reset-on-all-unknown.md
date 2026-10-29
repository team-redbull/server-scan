# ADR-0050: The OpenManage collector restarts the iDRAC of a Dell server whose disks or NICs all read UNKNOWN

Date: 2026-10-28
Status: Accepted (ships report-only; the job-queue states and Redfish paths are not yet verified live)

Refines ADR-0020 (the Dell collector) and ADR-0027 (`storage.all_unknown` / `network.all_unknown`).

## Context

Some Dell servers come back from the iDRAC with every drive health, or every NIC link state, UNKNOWN. ADR-0027
makes that an overall UNKNOWN: no verdict, and `/servers/available` drops the server. The operator's field
observation (2026-10-28): after an iDRAC restart (not a host reboot) the same server read UP/DOWN/OK again almost
every time, so the iDRAC's own inventory service is what had gone stale.

## Decision

- **A write against a BMC, from the collector run.** After `IngestService` persists a server, an optional
  `BmcResetService` (`app.application.services.bmc_reset_service`) checks `reset_trigger(server)`
  (`app.domain.services.bmc_reset`): a **reachable Dell collected by `OPENMANAGE`** whose `storage.all_unknown`,
  `network.all_unknown` or both is true. The trigger itself does not look at power; the resetter does (below).
  `REDFISH_STANDALONE` is out of scope (its per-host logins are not known to hold the privilege).
- **Same account as collection** (operator's call): `INVENTORY_OME_BMC_USERNAME`/`_PASSWORD`, which now needs the
  Configure Manager privilege to use this. The API pod still never holds it.
- **The job queue is checked first.** `IdracResetter` reads `/redfish/v1/JobService/Jobs?$expand=*($levels=1)` and
  skips the server when any job's `JobState` is not one of `Completed`, `CompletedWithErrors`, `Failed`,
  `RebootFailed`, `Scheduled`. An unknown or missing state blocks (it is treated as running). `Scheduled`, a
  change waiting for the next host reboot, does not block: it can sit for weeks, and an iDRAC restart does not
  cancel it. A skipped server is retried on the next run.
- **A powered-off host is powered on first** (2026-10-28, operator's call: a host that is off reads all-UNKNOWN and
  an iDRAC restart cannot fix that). After the job-queue check, `IdracResetter` reads the System's `PowerState`;
  on `Off` it POSTs `ComputerSystem.Reset {"ResetType": "On"}` and polls until `On` (120 s), and only then restarts
  the iDRAC. A host that does not come on in time is a `failed` event and the iDRAC is left alone. A host in
  maintenance is never powered on (it may be off on purpose): `failed`, nothing sent. Report-only only logs
  "would power the host on first". The event carries `power_state` (read before anything was sent) and `powered_on`, and the Events page says
  "Host was already on: only the iDRAC restart", "Host was off: powered on, then iDRAC restart" or "power-on sent, iDRAC not restarted". Needs the Power Control privilege too.
- **Resets never block the collection.** `IngestService.ingest()` only queues an eligible server (`_ResetWorker`);
  five background workers (`_RESET_CONCURRENCY`, distinct iDRACs) work the queue, so a 120 s power-on wait delays
  neither the next server's ingest nor, beyond five at once, the other resets. A cap slot is reserved before a reset
  and handed back when the server was in cooldown, so the per-run cap holds. The run's end waits for the queue for
  at most `ceil(maxPerRun / 5)` x 180 s, then logs
  `idrac_reset.drain_timeout` and drops what is left (retried next run). The worker sees the server as it was
  persisted, so a maintenance flag set seconds later is not seen.
- **The restart** is `POST <iDRAC manager>/Actions/Manager.Reset {"ResetType": "GracefulRestart"}`, sent once.
  `RedfishClient.post` never retries (a replayed action runs twice), and a dropped connection after the POST is
  outcome `unconfirmed`, not a failure, because the iDRAC going down is what a restart does. The run does not
  wait for it to return: the next collector run reads the recovered status.
- **Limits** (Helm `collectors.ome.idracReset`, env `INVENTORY_IDRAC_RESET_*`): **report-only by default**
  (reads the queue, logs `idrac_reset.not_sent` with `outcome=would_reset`, sends nothing),
  **a 12 h cooldown per server** and **at most 10 per run** (`0` switches it off). The cooldown is read from the audit
  trail (the newest `IDRAC_RESET` event for the server), so there is no new collection and no write to `Server`;
  a failed attempt starts the cooldown too. Report-only writes no event and so has no cooldown, but is still
  capped.
- **Every attempt is an audit event**, `IDRAC_RESET`, actor `ingestion`: `data` is `{trigger: disks|nics|both,
  outcome: requested|unconfirmed|failed, bmc_host, detail, active_jobs}`. A busy queue and report-only previews are
  logged only. The reset step never raises into the ingest.
- **`CLASSIFICATION_CHANGED` also carries the rename** (`name_from`, `name_to`) when the server's name changed in
  the same ingest. The event still fires only when the installation type changes.

## Consequences

- A collector run can now write to production hardware. The mitigations are report-only first, the per-run cap, the
  cooldown, the job-queue check and the audit event; the account's privilege is the last line.
- A server that is genuinely all-UNKNOWN for a reason an iDRAC restart cannot fix (a powered-off host, a dead
  controller) is restarted once per cooldown, indefinitely. The cap bounds the cost; the events make it visible.
- Verify live before setting `reportOnly: false`: read the report-only log of a real run for `would_reset` and for
  any `skipped_active_jobs` entry, and confirm the `JobState` values and that `$expand` is honoured (otherwise every
  job is fetched one by one).
