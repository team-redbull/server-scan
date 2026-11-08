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
| `ProcessorSummary.Model` | the CPU model, e.g. `Icelake-Server-v2` | absent |
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

A running VM is offered with a live recheck (`get_one` by `external_id`, about 4 s through the Route) and
`bmc_vendor: null`. A stopped VM is refused for `HEALTH` and `NETWORK` (ADR-0058, Consequences).

## Deploying the facade for a test

What worked: a Binary Docker `BuildConfig` from the fork's tree, the fork's Helm chart with
`rbac.clusterRole.create=false` and `rbac.clusterRoleBinding.create=false`, and a namespaced `Role`/`RoleBinding`
with the chart's rules minus `namespaces` and `storageclasses` (nothing on the read paths needs them; the pod
logged no `forbidden`). Nothing cluster-scoped is created, so deleting the namespace removes everything. The
Route's certificate is signed by the ingress CA:
`oc -n openshift-config-managed get cm default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}'`.
