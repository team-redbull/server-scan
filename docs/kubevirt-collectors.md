# KubeVirt collector: verified facts

The `KUBEVIRT` collector (ADR-0058) reads OpenShift Virtualization VMs through the team's kubevirt-redfish
fork. Everything below was observed on 2026-11-08 against OpenShift 4.22.13 with OpenShift Virtualization
4.22.12, the fork at `feat/system-identity-fields` (`1ba743c`) deployed in an isolated namespace behind an
edge Route, with three cirros VMs: two running, one `Halted`.

## Identity

- **Every VM has a firmware serial, a UUID and a machine type**, including a `Halted` VM that declared none:
  KubeVirt writes a random `firmware.serial` and `firmware.uuid` and the default machine type
  (`pc-q35-rhel9.8.0`) into the VM template within seconds of creation. They survive restarts.
- **Recreating a VM from a manifest without its firmware block gives it a new serial**, so the inventory sees
  a new server (the old document ages out through pruning). Recreating it from its exported spec keeps the
  serial, and the collector updates the existing document (`created=0`).
- The `metadata.uid` UUID fallback and the System-`Id` serial fallback never fired on this cluster; they exist
  for older facade builds and are covered by unit tests only.
- Under `system_id_convention: enhanced` a System `Id` is `<namespace>.<vm>`; `Name` is the bare VM name, so
  the site is parsed from the VM name as for any server.

## What a VM reports

| Resource | Running VM | Stopped VM |
|---|---|---|
| `PowerState` | `On` | `Off` |
| `Status` | `{Enabled, OK}` | `{Enabled, OK}` (not a verdict) |
| `Model` | machine type (`q35`, `pc-q35-rhel9.8.0`) | same, persisted in the template |
| `ProcessorSummary.Model` | the CPU model, e.g. `Icelake-Server-v2`, or `host-model` when the cluster sets no default (stored as no model, ADR-0059) | absent |
| `Oem.KubeVirt.NodeName` (fork change 11) | the node the VMI runs on | absent |
| `EthernetInterface.LinkStatus` | `LinkUp` | `NoLink`, MAC kept, no IPs |
| `Drive.CapacityBytes` | absent on a `containerDisk`, set on an `emptyDisk`/PVC | same, read from the spec |
| `Memory/1.CapacityMiB` | set | set (from the template) |

No drive has `Model`, `SerialNumber` or (on the virtio bus) `Protocol`; no interface has `SpeedMbps`. Every
sub-resource answers 200 for a stopped VM.

## Cost and latency

- One request through the Route took 0.9-1.3 s from outside the cluster, about 0.6-0.95 s of it TCP plus TLS.
  The client keeps connections alive; a full run over three VMs (28 requests) took 4.3-6.8 s at concurrency 4.
- Per VM: the System, `Memory` (2), `Storage` (2), each Drive, `EthernetInterfaces` and each member.
  `Processors` is read only when `ProcessorSummary` is absent. Each PVC-backed Drive costs the facade one live
  PVC GET behind client-go's 5 qps / burst 10.
- `@odata.etag` is `W/"<unix seconds>"` and changes every second whatever the content: useless for change
  detection.

## Failure behaviour, confirmed live

| Situation | What the collector does | Exit |
|---|---|---|
| Wrong password | one unauthenticated root GET, one `GET /Systems` answered 401, stop | 1 |
| `INVENTORY_KUBEVIRT_URL` unset | names the variable | 2 |
| Facade scaled to 0 | the Route answers 503; three tries on the open service root, no login spent | 1 |
| VM stopped between runs | updated in place: links `UNKNOWN`, overall `UNKNOWN`, CPU model cleared | 0 |
| VM deleted between runs | one fewer server, no error | 0 |

The facade logs a failed login only at DEBUG, so the client's own request log is the place to count 401s.

## `/servers/available`

A running VM is offered with `bmc_vendor: null`. On 2026-11-08 it was offered with a live recheck (`get_one` by
`external_id`, about 4 s through the Route); since ADR-0060 the hub API is not configured for `KUBEVIRT`, so a VM is
served from Mongo with no recheck (each cluster runs its own collector, and the hub cannot reach every facade). A stopped VM is refused for `HEALTH` and `NETWORK` (ADR-0058, Consequences).

## Deploying the facade for a test

What worked: a Binary Docker `BuildConfig` from the fork's tree, the fork's Helm chart with
`rbac.clusterRole.create=false` and `rbac.clusterRoleBinding.create=false`, and a namespaced `Role`/`RoleBinding`
with the chart's rules minus `namespaces` and `storageclasses` (nothing on the read paths needs them; the pod
logged no `forbidden`). Nothing cluster-scoped is created, so deleting the namespace removes everything. The
Route's certificate is signed by the ingress CA:
`oc -n openshift-config-managed get cm default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}'`.

## Second live pass: the platform's facade on cluster-2qsc5 (2026-11-08)

The redbull-platform hub cluster: OpenShift Virtualization 4.20.27, software emulation (`useEmulation: true`
on m5 workers, no KVM). The facade is the Argo-managed `kubevirt-redfish` app on the fork image
(`ghcr.io/team-redbull/kubevirt-redfish:sha-<commit>`), `system_id_convention: legacy` (the platform's
decision), chassis `vms` over namespace `server-scan-vms` with selector `redfish-enabled: "true"`. Three
cirros VMs (`ocp-tlv-kv-01..03`, two running, one `Halted`); server-scan ran as a separate test release.

- **Under `legacy` the System `Id` is the bare VM name**; `SerialNumber` is present, so the collector
  correlates on it and the `Id` fallback never fires.
- **The Route's certificate is publicly trusted** on this cluster: `verifyTls: true` with no
  `caBundleConfigMap`.
- **`host-model` is what a VM with no declared CPU model reports while running**, because neither the
  HyperConverged `defaultCPUModel` nor the KubeVirt `cpuModel` is set; a stopped VM reports none. Under
  emulation the CPU that mode resolves to is not the node's: an Intel Xeon Platinum 8259CL node is labelled
  `host-model-cpu.node.kubevirt.io/EPYC`, and the guest's libvirt domain says `EPYC`, vendor `AMD`.
- Run 1: `fetched=3 created=3 errors=0` in 0.3 s from inside the cluster; run 2: `unchanged=3`. Every
  sub-resource answered 200 for the halted VM.
- **`Oem.KubeVirt.NodeName` (fork `139404d`)** equals the VMI's `status.nodeName`
  (`ip-10-0-28-121.us-east-2.compute.internal`) and is absent for the halted VM. Stored as `ip-10-0-28-121`.
  The first run after upgrading to ADR-0059 was `updated=3` (each VM gained `vm_host`, and `host-model`
  became no model), the next `unchanged=3`. `GET /servers/{id}/vm-host` answered `NOT_IN_INVENTORY` for the
  running VMs (this cluster's nodes are AWS instances, not inventoried servers) and `NOT_RUNNING` for the
  halted one; a `MEMBERSHIP` match has not been seen live.
