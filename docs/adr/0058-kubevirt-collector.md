# ADR-0058: A sixth collector, `KUBEVIRT`, inventories OpenShift Virtualization VMs through kubevirt-redfish

**Status:** Accepted, implemented 2026-11-08; live pass against OpenShift Virtualization 4.22.12 the same day
(`docs/kubevirt-collectors.md`).

**Extends** ADR-0011 (the closed vendor set grows by `kubevirt`), ADR-0023 (a new provider behind the same
ABC) and ADR-0032 (`get_one` for `/servers/available`'s recheck).

## Context

The operator provisions OpenShift Virtualization VMs the same way as bare metal: Metal3 drives them through
kubevirt-redfish, a Redfish facade over KubeVirt where a namespace is a Chassis and a VM is a ComputerSystem.
Those VMs were invisible to the inventory. The team's fork of the facade (team-redbull/kubevirt-redfish, upstream
kubevirt/redfish-controller plus merged open PRs) serves per-System `Memory`, `Processors`, `Storage/Drives` and
`EthernetInterfaces` (upstream PR #25), enough to fill the same `ProviderServer` the other collectors fill.

Three facts about the facade shaped the design:

- **HTTP Basic only.** The service root has no `RedfishVersion` and no Sessions, so `RedfishClient`
  (conformance check, session login, https-only target) cannot be reused; its exception hierarchy and the pure
  mapping helpers can.
- **10 failed logins per IP in 5 minutes block that IP for 15 minutes.** A rejected login must never be retried.
- **One endpoint serves every VM**, and a VM has no BMC of its own: `Status` is always `{Enabled, OK}`.

## Decision

1. **`Vendor.KUBEVIRT = "kubevirt"`, `ManagerType.KUBEVIRT`.** Not `standalone`: a VM is not a manufacturer
   this platform fails to model. `bmc_vendor` (the bmhgen driver string) is `null` for it.
2. **Identity.** The fork's `ComputerSystem` gained `SerialNumber`, `UUID`, `Manufacturer` and `Model` (its
   commit `1ba743c`, "Change 10" in its `SCALEFIX-CHANGES.md`): the VM's `firmware.serial`/`firmware.uuid`
   (running VMI first, then the template; UUID falls back to `metadata.uid`) and machine type. The collector
   correlates on `SerialNumber`, falling back to the System `Id` (`<namespace>.<vm>` under the facade's
   `enhanced` convention) for an older build. Recommend `enhanced`: under `legacy` two chassis with a
   same-named VM collide.
3. **One login attempt per run.** `health_check` reads only the unauthenticated service root; `_list_servers`
   makes one authenticated `GET /redfish/v1/Systems` before any concurrency, so a wrong password costs one
   401. A 401 anywhere (listing, mid-run, recheck) is never retried; mid-run it stops the run.
4. **Failure split.** The listing failing (unreachable, TLS, 401, any error status) fails the run: exit 1.
   One VM failing is `_record_error`ed and the run continues (exit 3); a 404 on a VM is "deleted between
   listing and reading" and is skipped silently. **No `reachable=False` stub**: the "BMC" is the shared
   facade, so a VM is always `reachable=True` and the `bmc` health category does not apply.
5. **A stopped VM's links are `UNKNOWN`, not `DOWN`** (operator, 2026-11-08). The facade says `NoLink` for a
   powered-off VM; that is not a cable fault. With every link UNKNOWN the VM is UNKNOWN overall
   (`network.all_unknown`), never CRITICAL "all links down". A running VM's `NoLink` (a disconnected NIC) stays
   `DOWN`.
6. **`network.single_link_up` is scoped to the physical collectors** (`manager_types` lists all six physical
   types, `UCS_MANAGER` included). A one-NIC VM is normal. `bootstrap` re-syncs system defaults on every API
   start, so the new scope reaches existing deployments without a migration.
7. **"Read, none" for what a VM never has:** `psus=()`, `gpus=()`, template `""`. `raid_check` stays `None`
   (as for Intersight and OneView) and RAID is outside its scope. A VM left on the cluster-default CPU reports
   `cpu_model=""`, which ingest now stores as `None` without flagging it unread (it already did this for
   templates). A `Memory/1` with no `CapacityMiB` (a stopped instancetype VM) stays `None`: its size is unknown,
   not zero.
8. **Budgets and concurrency.** `INVENTORY_KUBEVIRT_CONCURRENCY` (4), a per-VM budget (60 s), a run budget
   (1800 s) and a 35 s read timeout, just above the Route's 30 s so a stall surfaces as the Route's 504. Four
   VMs in flight, each with at most two PVC-backed disks, stay under client-go's 5 qps / burst 10 in the facade.
9. **Helm `collectors.kubevirt`**, off by default; every key read through `default dict` (the platform repo's
   `values.yaml` lacks the block). `caBundleConfigMap` mounts the ingress CA in the CronJob and the API pod.

## Consequences

- **A stopped VM is never offered by `/servers/available`**: `SELECTABLE_TIERS` excludes UNKNOWN and the
  network health must be HEALTHY. The endpoint assumes powered-on hardware. Whether to offer stopped VMs (a
  PowerState-aware tier, or a `KUBEVIRT` exemption) is open for the operator to decide.
- **`PowerState` has no field in `ProviderServer`**; it shapes link state only.
- **Identity drift mints a new document**, as a vendor move does (ADR-0011): adding `firmware.serial` to a VM
  later, upgrading from a facade build without `SerialNumber`, or recreating a VM from a manifest without its
  firmware block (KubeVirt then writes a new random serial).
- **Rollback:** an image older than this one cannot load a `kubevirt` server (`Vendor` and `ManagerType` are
  validated enums), nor a health policy or classification rule whose scope names `KUBEVIRT`, and a bad policy
  breaks evaluation for every server. Before rolling back, delete servers with `source_provider: KUBEVIRT` and
  any rule or policy scoped to it. The old image's bootstrap re-syncs `network.single_link_up` to unscoped, harmlessly.
- `GET /sites`' `by_vendor` lists every `Vendor`, so it gains a `kubevirt` entry (zero where there are none).
- **Lockout:** the collector and the API pod's rechecks may share an egress IP; a misconfigured API pod
  could lock the collector out for 15 minutes.
- The Architecture page has no KubeVirt diagram yet.

## Amended by ADR-0059 (2026-11-08)

- A VM now stores the cluster and node it runs on (`Server.vm_host`), from the fork's `Oem.KubeVirt.NodeName`.
- `host-model` and `host-passthrough` are libvirt CPU modes, not models; the collector stores them as `""`,
  like an undeclared model (point 7), so a VM no longer flips between `host-model` and `null` on start/stop.
- Point 2's "recommend `enhanced`": the redbull-platform facade runs `legacy` by its own decision (2026-11-05).
  Harmless with the fork's `SerialNumber`, which the collector correlates on before the System `Id`.
