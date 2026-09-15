---
paths:
  - "backend/app/infrastructure/providers/**"
  - "backend/app/infrastructure/credentials/**"
  - "backend/app/domain/ports/provider.py"
  - "tools/run_collector.py"
  - "backend/app/infrastructure/providers/factory.py"
  - "tools/verify_*.py"
  - "docs/*-collectors.md"
  - "deploy/helm/server-scan/templates/*collector*"
---

# Collectors — read before touching a provider

Loaded only when a collector file is open. Full detail is in the ADR each
item names; the dated validation history is in
`docs/notes/2026-09-13-claude-md-archive.md`.

## The architecture

There is no single sync process. Each vendor gets a
`ServerInventoryProvider` (`app.infrastructure.providers.<vendor>`, the seam
`app.domain.ports.provider` defines and `providers.fake` exercises), and each
manager *type* gets a Kubernetes `CronJob` running
`tools/run_collector.py --manager-type <TYPE>`. A run resolves that type's
endpoint + login from settings (`EnvConnectionResolver`: one endpoint, one
login per type — that is the whole connection config; a half-configured
vendor raises `ManagerNotConfiguredError` naming the variables), talks to
the vendor API, normalizes into `ProviderServer`, and feeds `IngestService`
— classify, health-evaluate, audit, upsert, one write per server. A
`Manager` document is written per run as a *projection* of that config
(`manager_for`), never its source. A collector never talks to the API.

**`UCS_MANAGER` is the one carve-out: a login with no endpoint** and no
CronJob — `UCS_CENTRAL` discovers every domain's address at runtime and logs
into each with `INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD`. Its absence
from `PROVIDER_FACTORIES` is deliberate.

Five collectors: `UCS_CENTRAL`, `INTERSIGHT`, `OPENMANAGE`, `ONEVIEW`,
`REDFISH_STANDALONE`. All five have had a live field pass and every one
found at least one defect the contract alone could not (ADR-0009/0014,
0017, 0020, 0022, 0016 — their dated updates).

## The provider contract

- **`None` means "could not read this run"; `()`/`0` means "read, none".**
  `IngestService` carries the stored value forward for `None` and records
  the path in `Server.unread_fields` (rebuilt every ingest, never merged).
  Collapsing the two once wrote zeros over good data and reported a failed
  drive as recovered (ADR-0016).
- **`reachable=False`** = identity known, BMC not reached; every optional
  field is `None`, nothing is blanked, it does not count toward
  `collection_errors`. `OPENMANAGE` writes it for a dead *or*
  auth-rejected iDRAC; `REDFISH_STANDALONE` writes none (no serial to
  correlate on).
- **No `site_id`** — parsed from the name at ingest. **`nic_macs` is the
  minimum; `nics` the richer view.** `interface_kind` is `PHYSICAL` or
  `VNIC`; only PHYSICAL counts as a fabric path.
- **`collect()` is the template method** (ADR-0023): resets
  `collection_errors`, wraps `_list_servers()` in `aclosing`. Call
  `super().__init__()`. `_list_servers` is a plain `def` returning
  `AsyncGenerator` — ADR-0023 says why an `async def` stub breaks ty.
- **`get_one(ServerIdentity) -> ProviderServer | None`** (ADR-0032) is the
  sixth abstract method, for `GET /servers/available`'s live recheck.
  Fetch that one server directly — a scoped `$filter` (Intersight), a
  direct-by-URI/DN read (OneView; UCS Manager's `query_dn(hierarchy=True)`),
  a single-host recollect (Redfish; OpenManage via a one-off target), or
  a small directory query plus per-domain delegation (UCS Central).
  **Never `_list_servers()` re-run and filtered.** `FakeProvider` has a
  test-only `get_one_overrides`. Intersight's owner-relation `$filter`s
  are unverified live.
- **Provider construction lives in `app.infrastructure.providers.factory`**
  (`PROVIDER_FACTORIES`, `build_provider`, `build_provider_for_manager_type`,
  `manager_for`, `resolve_name_pattern`), not in `tools/run_collector.py`,
  which imports it. `app` must never import `tools`: it is not an
  installed package, and `uvicorn --app-dir backend` (the README's dev
  command) cannot see it — that import shipped once (2026-09-13) and
  worked only through uvicorn's default `--app-dir .`.
- **Every collector reports PSUs.** An `Absent` supply is dropped, never
  failed; a PSU `health` is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, **never a
  `HealthSeverity`** — this exact confusion has shipped three times, last
  in OneView (fixed 2026-09-07). Redfish `Warning` -> `UNKNOWN` on purpose.
- **GPU VRAM**: only Redfish (and so Dell) reads it; OneView/Intersight/UCS
  hardcode `None` and `GpuCatalog` fills it in at ingest (ADR-0021). A
  value a provider read is never overridden.
- **A UCS server's name comes from its service profile**, never
  `computeBlade.name`. **Cisco vNICs populate `nics`** (2026-09-10) with
  `link_state=UNKNOWN` on ~99.75% of real ones — that is the field's
  meaning, not a bug; `cisco_eno_names` names them `eno5, eno6, …`.
- Vendor SDKs are synchronous — go through `run_abandonable`
  (`app.infrastructure.blocking`), never call them from the loop.
  **`ucsmsdk` 0.9.18's ~32 `SyntaxWarning`s are filtered** (pytest
  `filterwarnings`, `PYTHONWARNINGS` in the Containerfile), scoped to the
  one message; `W605` in the ruff gate keeps our own escapes honest.

## Name filtering

A collector only ingests names matching `INVENTORY_COLLECTOR_NAME_PATTERN`
(`^ocp`; empty = everything), applied as `_NameFilteredProvider` in
`run_collector.py`, not in `IngestService` (so `--dry-run` cannot lie).
`REDFISH_STANDALONE` is exempt from the global (`UNFILTERED_TYPES`) — a
BMC does not know the name; its inventory file is the filter. Per-type
overrides (`INVENTORY_<TYPE>_NAME_PATTERN`) are `str | None`: unset
inherits, **explicitly empty opts out**, and an override beats the Redfish
exemption. **All of it is reconciled in `resolve_name_pattern` and every
reader goes through it** — three collectors prune on the pattern before the
wrapper sees anything (OME skips BMCs, Central skips domains, OneView skips
per-server calls), so a factory reading `Settings` itself would prune on
the global and filter on the override, silently.

## Per-vendor decisions a session is most likely to get wrong

- **`INTERSIGHT`** (ADR-0017): not a login — every request is signed
  (`INVENTORY_INTERSIGHT_API_KEY_ID`/`_API_KEY_PEM`, hand-rolled on
  `httpx`+`cryptography`, no key file); cost flat in fleet size (~120
  requests for 10k servers, join tables in memory, `$select` everywhere);
  deliberately skips `ManagementMode == UCSM` because `UCS_CENTRAL` owns
  those and both writing one document would flip it per run
  (`INVENTORY_INTERSIGHT_MANAGEMENT_MODES` overrides). Reachable from the
  air-gapped site only via the on-prem appliance. `TotalMemory` is MiB.
  Open: the DOWN/CRITICAL side of its `"OK"` vocabulary; `FlexUtil`/
  `FlexFlash` boot storage (real, not implemented).
- **`OPENMANAGE`** (ADR-0020, `docs/dell-collectors.md`): identity from two
  OME bulk calls, hardware from each iDRAC over Redfish — two logins
  (`INVENTORY_OME_*` + `INVENTORY_OME_BMC_*`), refuses to start without
  both. Service Tag is `Oem.Dell.DellSystem.NodeID`, **not**
  `ComputerSystem.SerialNumber` (confirmed on iDRAC9 only); BMC address is
  `DeviceManagement[0].NetworkAddress`, **not** `TargetName`/`DeviceName`
  (OME's "Server Device Naming" setting can make those an OS hostname).
- **`REDFISH_STANDALONE`** (ADR-0016): unreachable host or rejected login
  is ERROR-logged but exit-0-safe (`_is_benign_collection_error`); TLS
  failure, blown host budget, unrecognised error stay PARTIAL.
  **Nothing skips a BMC (2026-09-12)** — the `_AuthGuard` breaker is
  deleted at the operator's explicit direction; the lockout risk (Lenovo
  XCC, hour-long iDRAC IP block) is theirs. Do not re-add without asking.
  **A GPU-baseboard tray is "has a GPU, no CPU," not "every processor is
  a GPU"** (2026-09-15) — a real tray's FPGA/NVSwitch companions in
  `Processors` must not disqualify it; `has_only_gpu_processors` still
  treats a missing `ProcessorType` as a CPU, matching `cpu_summary`'s own
  default. **A GPU reported only as a `PCIeDevice`** (never `Processors`)
  needs `INVENTORY_REDFISH_PCIE_GPU_DETECTION=true`, off by default — see
  ADR-0016's 2026-09-15 update for the vendor-ID+description heuristic
  and its confidence level (NVIDIA confirmed live; AMD/Intel are not).
  **Two distinct real shapes exist and both are checked**:
  `Chassis.PCIeDevices` (a real collection, `$expand`-eligible) and
  `ComputerSystem.PCIeDevices` (a direct link array — confirmed live on
  a DGX H100, where named-property `$expand` on it silently returned
  zero of 133 real entries). Never trust an empty `$expand` result
  without checking `Members@odata.count` first. **A PCIeDevice's
  `Manufacturer` can pack a raw PCI vendor+device ID** (`"10DE20B2"`,
  not a real name) — `_NVIDIA_PCI_DEVICE_MODELS` resolves the ones
  cross-checked against the PCI ID Repository (pci-ids.ucw.cz — not a
  Cisco list; PCI-SIG assigns vendor IDs, this repo aggregates every
  vendor's own device IDs) to a `GpuCatalog`-matchable string so VRAM
  enriches automatically. **PSU `power_watts` reads a separate linked
  `PowerSupplyMetrics.InputPowerWatts`**, never a property on
  `PowerSupply` itself — the same "telemetry lives one link away"
  pattern GPUs already use.
- **`ONEVIEW`** (ADR-0022, `docs/hpe-collectors.md`): **one collection
  standard for all HP hardware, whatever its iLO generation** — no Redfish
  pass, no BMC credentials, `mpModel` reported but never branched on. An
  explicit operator decision for a mixed iLO 4/5/6 estate; not up for
  re-litigation in code. Three bulk calls (`expand=all`) plus bounded
  per-server `/powerSupplies` and `/processors` fallbacks, each switchable
  off; `/processors` is the only source of `cpu_threads`. Traps: the name
  comes from the **profile** (`server-hardware.name` is a bay location);
  `cpu_cores = processorCount * processorCoreCount`; `count=-1` means 64
  with a 256 ceiling; `InsufficientFirmware`/`CollectedStale` are `None`,
  not zero.

## The research bar for any vendor work

Research that vendor's *current* API docs and the installed SDK's source
directly; never trust this file's or an older note's specifics without
reconfirming. UCS Manager's build cross-checked every attribute against
the installed `ucsmsdk`; OneView's read HPE's API Reference and the
`hpeOneView` SDK source. Testability without hardware decided the build
order — two of four vendors had no test target at all. Any new vendor gets
`get_one` alongside `_list_servers`, a factory entry, a CronJob, and a
`fake` shape (convention 10).
