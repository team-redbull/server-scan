# ADR-0048: RAID 1 detection reports present, absent or unknown, decided by the collector

Date: 2026-10-13
Status: Accepted

## Context

The operator wants a server without a RAID 1 for its OS to be CRITICAL. Redfish does not give one
answer across the fleet (live tests on the operator's BMCs, 2026-10-13; sources in
`docs/notes/2026-10-13-dell-redfish-storage-volumes.md` and `docs/notes/2026-10-13-standalone-redfish-raid.md`):

- **Dell iDRAC9/10:** `Systems/System.Embedded.1/Storage/<id>/Volumes` is complete. A BOSS card with
  `Members@odata.count: 0` really has no mirror.
- **NVIDIA DGX A100/H100 (AMI MegaRAC):** `Storage` lists NVMe/USB controllers and a ~127 MB boot `VOL0`
  with no `RAIDType`. The OS mirror is `mdadm` software RAID and cannot be seen over Redfish.
- **HPE iLO 5 (Gen10):** `Systems/1/Storage` is empty; RAID lives only under
  `Systems/1/SmartStorage/ArrayControllers/<id>/LogicalDrives` (`Raid: "1"`). iLO 6 has `Storage` only.
- **Old Supermicro BMC:** `Storage` answers 503, `SmartStorage` 404: nothing can be read.

Reading "no volume" as "no RAID" would mark every DGX and every old BMC CRITICAL.

## Decision

The Redfish collector (`providers/redfish/raid.py`, used by `OPENMANAGE` and `REDFISH_STANDALONE`) stores
`hardware.storage.raid = {state, method, bmc, reason}`; the policy only reads `state`.

- **PRESENT** whenever any source shows a mirror: `RAIDType` RAID1 (any case), or `VolumeType` Mirrored with a
  null `RAIDType`, or an HPE logical drive `Raid` of `1`/`1ADM`/`1Triple`. Positive evidence is safe on every vendor.
  RAID10, RAID1E, raw devices and `SupportedRAIDTypes` never count.
- **ABSENT** (the CRITICAL policy `raid.os_mirror_missing`) only where the vendor tree is authoritative and fully read:
  Dell (`Storage` and every `Volumes` collection read), HPE (SmartStorage read with a controller, or, on iLO 6, a
  `Storage` whose members all have `Volumes`).
- **UNKNOWN** for everything else, with a `reason`: DGX/AMI, Supermicro, Lenovo, OpenBMC, unknown makers, an empty
  `Storage` with no SmartStorage, a 404/501/503 on the storage schema. A non-Dell member without a `Volumes` link
  is never "no volumes".
- **Unread (`None`, stored value kept, flagged in `unread_fields`)** on a transient failure (timeout, 403, other 5xx)
  while no mirror was seen. A failed read must never become "no RAID".
- `bmc` ("iDRAC", "iLO 5", "AMI MegaRAC") is a display label from the service root, never a decision input.
- The `raid` health category is UNKNOWN when the state is UNKNOWN, so an unknowable server is never a verdict.

## Consequences

- A DGX or old Supermicro shows RAID "Unknown" with its reason; it never fires the alert.
- Dell with only AHCI/NVMe/HBA and no PERC/BOSS is ABSENT (Dell's tree is complete). A Dell with S150/S160
  software RAID is not verified (Dell documents no Redfish representation) and could read ABSENT: check on the
  first such server.
- A RAID 1 on a PERC data volume satisfies the rule; Redfish has no reliable boot-volume flag
  (`BootVirtualDiskFQDD` was null on most captures).
- Not verified on real hardware: the HPE `Oem.Hpe.Links` names and real `Raid` strings, iDRAC10 BOSS ids, a configured
  BOSS mirror under `$expand`. Each is a one-line GET listed in the two research notes.
- One extra request per storage controller, plus SmartStorage reads on HPE.

## Update (2026-10-13, later): Cisco UCS

`UCS_CENTRAL` (and its per-domain `UCS_MANAGER`) now report the same three states from UCS Manager's own
inventory (`storageController` + `storageVirtualDrive`, `docs/cisco-collectors.md`, "Storage controllers and
RAID"). UCS Manager lists every virtual drive of a server, so `ABSENT` is allowed for a RAID-capable
controller with no `type="mirror"` drive; no RAID-capable controller visible is `UNKNOWN`. The policy scope is
now OPENMANAGE, REDFISH_STANDALONE, UCS_CENTRAL and UCS_MANAGER; Intersight and OneView stay unjudged.
