# ADR-0059: A KubeVirt VM records the node it runs on; the host's server record is resolved when read

**Status:** Accepted, implemented 2026-11-08.

**Extends** ADR-0058 (the `KUBEVIRT` collector) and reuses ADR-0024/0036 (the `nodes-status` membership and
its hostname match).

## Context

A VM's CPU model, as KubeVirt reports it, is either a named family (`Icelake-Server-v2`), or a libvirt *mode*:
`host-model` or `host-passthrough`. A mode is "copy the node's CPU", not a model. On cluster-2qsc5 every VM
without a declared model reported `host-model`, because neither the HyperConverged `defaultCPUModel` nor the
KubeVirt `cpuModel` was set. A stopped VM of the same spec reported no model at all, so the stored value would
flip between `host-model` and `null` on every start and stop.

The physical CPU behind a VM is not available from Kubernetes in any useful form. KubeVirt's node labels carry a
family name (`host-model-cpu.node.kubevirt.io/<family>`), and under software emulation a wrong one: on
cluster-2qsc5 an Intel Xeon Platinum 8259CL node was labelled `EPYC`. No Node field carries the brand string.
This platform already holds the real CPU, read from the node's own BMC, and already knows which bare-metal
server is which OpenShift node: the `nodes-status` jobs write `openshift.cluster_name` on each `INSTALLED`
server, and the hostname is `name_normalized`, or `openshift.reported_name` when the two differ (ADR-0036).

## Decision

1. **The facade reports the node.** The kubevirt-redfish fork (change 11, commit `139404d`) serves
   `ComputerSystem.Oem.KubeVirt.NodeName`: the running VMI's `status.nodeName`, omitted when the VM is not
   running. Redfish has no standard property for a ComputerSystem's hypervisor host.
2. **A VM stores names only: `Server.vm_host = {cluster_name, node_name}`**, `None` for every physical
   server.
   - `node_name` is cleaned with `clean_hostname`, the function `nodes-status` uses, so it compares equal to
     what the membership jobs match on.
   - `cluster_name` is `INVENTORY_KUBEVIRT_CLUSTER_NAME` (Helm `collectors.kubevirt.clusterName`): one facade
     serves one cluster. It must be the same string that cluster's `nodes-status` release uses.
   - Read semantics follow ADR-0016: a VM `Off` reports `""` (no node, stored `None`). A running VM whose facade
     omits the property reports `None`, so the stored node is carried forward and `vm_host.node_name` is listed
     in `unread_fields`.
3. **The host is resolved when read, by `GET /servers/{id}/vm-host`, and never copied onto the VM.**
   - A copy would make the VM collector depend on another collector's data and run order, and it would go
     stale whenever the host is re-collected.
   - `GET /servers/{id}` is cached per VM revision, so the resolved host is not embedded there either; the
     detail carries only the stored names.
   - The endpoint does one indexed lookup per call and is never cached.
   - The match order:
     1. **`MEMBERSHIP`:** an `INSTALLED` server in `cluster_name` (any cluster when unset) whose
        `reported_name` equals the node, or whose `name_normalized` does when it has no `reported_name`.
     2. **`NAME`:** failing that, a server whose `name_normalized` equals the node. This covers a cluster with
        no `nodes-status` release.
     3. **`AMBIGUOUS`** when either step finds two candidates; **`NOT_IN_INVENTORY`** when neither finds any;
        **`NOT_RUNNING`** when the VM has no node.
   - A VM is never a candidate. The response carries the host's id, name, vendor, model, site, CPU, memory,
     overall health, maintenance flag and cluster.
4. **A CPU mode is not a model.** The collector stores `host-model` and `host-passthrough` as `""` (stored
   `None`), like a VM with no declared model. A running and a stopped VM of the same spec now store the same
   value, and the physical CPU is read from the host instead.
5. **Not in `/servers/rows`.** The inventory list stays one projection with no join (ADR-0033); the host is
   shown on the VM's Overview only.

## Consequences

- **A live migration changes `node_name`, so it is a content change:** a new revision and an `updated` in the
  run summary. Between 3-hourly runs the stored node can be stale; the Overview shows it as of the last run.
- **A cluster whose nodes are not in the inventory** resolves every VM to `NOT_IN_INVENTORY`. That is the
  answer, not an error. cluster-2qsc5 (AWS nodes) is such a cluster, so this resolution was verified through
  tests and the fake fleet, not live.
- **`cluster_name` drift:** if `collectors.kubevirt.clusterName` differs from the `nodes-status` release's
  name, the membership step finds nothing and a unique hostname still resolves as `NAME`. A wrong name is
  visible as "matched by name only" in the UI.
- **The fake fleet:** each running fake VM names a physical server of its own site as its node, in cluster
  `upi-<site>`. Hosts that really are `INSTALLED` there resolve as `MEMBERSHIP`, the rest as `NAME`; 5% of
  running VMs name a node that does not exist.
- **Stored shape:** one optional field. An older image ignores it (pydantic's default `extra="ignore"`), so a
  rollback needs no cleanup beyond ADR-0058's.
- **Facade dependency:** a facade build without change 11 leaves every running VM's node unread. The VM is
  still collected, and its stored node is carried forward.
