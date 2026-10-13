# Dell Redfish storage volumes: detecting a RAID 1 OS mirror (iDRAC9 and iDRAC10)

Researched 2026-10-13. No production code. Scratch copies of every downloaded artifact are under the session
scratchpad (`.../scratchpad/g/`).

## Summary

The design (Storage collection -> per-member `Volumes` collection -> `RAIDType == "RAID1"`) is the same walk Dell's
own scripts and Ansible module use, and it works unchanged on iDRAC9 7.x and iDRAC10 1.x. The `Volumes` link sits at
`Storage/<Id>/Volumes` on both, the `RAIDType` enumeration is the unmodified DMTF/Swordfish list, and the Dell-published
mockups of iDRAC10 R770 and iDRAC9 R660xs/R650/R760xa show it. Four things in the design need correcting or hedging:

1. **Id-prefix classification mislabels BOSS-S2**: a BOSS-S2 appears as `AHCI.SL.6-1` (its `Name` is `BOSS-S2`), and
   BOSS-N1 appears as `BOSS.Integrated.1-1` (modular) or `BOSS.SL.16-1` (monolithic). Dell's own test code finds BOSS by
   **`Name` containing `BOSS`**, not by Id. HBAs are `NonRAID.*`, NVMe extenders are `PCIeSSD.*`/`PCIeExtender.*`.
   Impact is label-only because the RAID1 rule does not read `kind`.
2. **There is no boot/OS-volume flag on a Dell Volume.** Dell's served `Volume.v1_10_0` has neither `BootVolumeSource`
   nor DMTF's `IsBootCapable`. The only Dell indicator is `Oem.Dell.DellController.BootVirtualDiskFQDD` on the *Storage
   member*, a per-controller legacy boot-VD setting that is `null` in 2 of the 3 PERC captures and set (to a RAID0 VD)
   in the third. A RAID1 data VD on a PERC is therefore **not** distinguishable from an OS VD by any reliable field.
3. **`RAIDType` is null for every non-RAID "RawDevice" volume** (BOSS and NVMe single disks), never the string
   `"None"` in any capture, so the existing `or None` handling is right. Old iDRAC9 firmware (3.x) has no `RAIDType` at
   all, only `VolumeType`; the property first appears in Dell's guide at 4.20.20.20.
4. **A BOSS card (and an NVMe-only or HBA-only server) with no RAID1 is indistinguishable, in these captures, from a
   server that legitimately has no RAID** (OS-level mdadm/ZFS, SAN boot, diskless). `raid.os_mirror_missing` is
   CRITICAL on any controller-bearing server with zero RAID1 volumes; see section 6 for the recommendation.

Not confirmed from any primary source: how S150/S160 software-RAID virtual disks appear (Id prefix, whether a RAID1
shows as a Volume), how a Foreign configuration appears, whether a Failed VD stays in the collection, and BOSS-N1 naming
on 17G (no BOSS-bearing iDRAC10 capture is published). See the last section.

## Sources used

| Tag | Source |
|---|---|
| MOCK-10G | Dell `iDRAC-Redfish-Scripting` repo, `iDRAC Redfish Mockup Clients/R770_iDRAC10_1_10_17_00_mockup_client.zip` (iDRAC10 fw 1.10.17.00; 17G R770; Storage.v1_16_0, Volume.v1_10_0). https://github.com/dell/iDRAC-Redfish-Scripting/tree/master/iDRAC%20Redfish%20Mockup%20Clients |
| MOCK-10G-AMD | same dir, `R7725_iDRAC10_1_20_70_50_mockup_client.zip` (iDRAC10 1.20.70.50; Storage.v1_18_0, Volume.v1_10_1) |
| MOCK-10G-XE | same dir, `XE9712_iDRAC10_1_30_07_10_mockup_client.zip` (iDRAC10 1.30.07.10; Storage.v1_19_0, Volume.v1_10_2) |
| MOCK-9G-16 | same dir, `R660xs_iDRAC9_7_20_10_05_mockup_client.zip` (iDRAC9 fw 7.20.10.05; Storage.v1_16_0, Volume.v1_10_0) |
| MOCK-9G-16B | same dir, `iDRAC_mockup_client_GPU_DPU_config.zip` (R760xa, `Managers/iDRAC.Embedded.1` FirmwareVersion `7.10.50.00`; Storage.v1_15_0, Volume.v1_9_0) |
| MOCK-9G-15 | same dir, `iDRAC_mockup_client_basic_config.zip` (R650, FirmwareVersion `7.10.30.00`; Storage.v1_15_0, Volume.v1_9_0) |
| SCRIPTS | same repo, `Redfish Python/GetStorageInventoryREDFISH.py`, `CreateVirtualDiskREDFISH.py`, `SetBootVdREDFISH.py`, `Redfish PowerShell/Invoke-StorageImportClearForeignConfigREDFISH` |
| ANSIBLE | Dell `dellemc-openmanage-ansible-modules`, https://github.com/dell/dellemc-openmanage-ansible-modules @ `16345e5` (2026-10-08): `plugins/modules/redfish_storage_volume.py`, `roles/redfish_storage_volume/molecule/__extract_storage.yml` |
| FAQ | Dell white paper "Frequently Asked Questions About iDRAC Redfish", Dec 2024, https://dl.dell.com/content/manual16004247-idrac-redfish-faq.pdf (covers iDRAC9 and iDRAC10) |
| WP2018 | Dell white paper "Standards-based storage management for Dell EMC PowerEdge servers using the iDRAC RESTful API and DMTF Redfish", June 2018, https://dl.dell.com/manuals/all-products/esuprt_software/esuprt_it_ops_datcentr_mgmt/dell-management-solution-resources_White-Papers10_en-us.pdf |
| GUIDE-3.36 | https://dl.dell.com/topicspdf/idrac9-lifecycle-controller-v33-series_api-guide2_en-us.pdf (iDRAC9 3.36.36.36 Redfish API Guide, 2020-01) |
| GUIDE-4.20 | https://dl.dell.com/topicspdf/idrac9-lifecycle-controller-v4x-series_api-guide_en-us.pdf (iDRAC9 4.20.20.20 Redfish API Guide, July 2020) |
| RN-4.20 / RN-5.00.10 | https://dl.dell.com/topicspdf/idrac9-lifecycle-controller-v4x-series_Release-Notes_en-us.pdf ; .../idrac9-lifecycle-controller-v5x-series_Release-Notes2_en-us.pdf |
| DMTF | https://redfish.dmtf.org/schemas/swordfish/v1/Volume.json and `Volume.v1_10_0.json` (fetched 2026-10-13) |

The mockups are Dell-published crawls of iDRAC Redfish trees including the iDRAC's own served `Schemas/` copies. They
are Dell's own data but a crawl artifact is possible; where a mock looks odd it is marked as such.

The Dell developer portal (https://developer.dell.com/apis/2978, "iDRAC9 Redfish API" versions 7.xx/6.xx/5.xx/4.xx, and
the iDRAC10 entry) is a JavaScript single-page app: `openapi.yaml` URLs return the HTML shell (HTTP 200, 160 KB of
HTML), so the **current iDRAC9 7.xx and iDRAC10 API reference could not be read**. Their content is covered here only
through the firmware-served schemas and example trees in the mockups, which are equivalent for these resources.

## 1. Does the walk work on iDRAC9 and iDRAC10?

### Storage collection and member Ids

- iDRAC10 1.10.17.00 (R770, PERC H965i): collection `Members` = `.../Storage/RAID.SL.3-1`, `Members@odata.count: 1`,
  Storage `Name` "PERC H965i Front", `@odata.type` `#Storage.v1_16_0.Storage`. (MOCK-10G,
  `redfish/v1/Systems/System.Embedded.1/Storage/index.json` and `.../RAID.SL.3-1/index.json`.)
- iDRAC9 7.20.10.05 (R660xs): members `RAID.SL.5-1`, `AHCI.Embedded.1-1`, `AHCI.Embedded.2-1`, `BOSS.Integrated.1-1`
  (MOCK-9G-16). So **Id patterns did not change between iDRAC9 and iDRAC10** in the captures; the 17G R670 itself is not
  published.
- The BOSS-N1 member is `BOSS.Integrated.1-1`, `Name` "BOSS-N1 Modular" on 7.20 (MOCK-9G-16) and `BOSS.SL.16-1`,
  `Name` "BOSS-N1 Monolithic" on 7.10.50 (MOCK-9G-16B). **iDRAC10 BOSS naming is UNVERIFIED** (no BOSS capture on iDRAC10).
- **BOSS-S2 is `AHCI.SL.6-1`** with `Name` "BOSS-S2", `StorageControllers[0].Model` "BOSS-S2",
  `SupportedRAIDTypes: ["RAID1"]`, 2 drives (MOCK-9G-15, `.../Storage/AHCI.SL.6-1/index.json`). The same capture's
  `BootOptions/Boot0010` is "AHCI Controller in SL 6: VMware ESXi", consistent. An Id-prefix rule (`AHCI.` -> "plain SATA")
  mislabels it.
- Dell's own Ansible molecule tests locate the BOSS controller by name, not Id:
  `redfish_storage_volume_search_in_name: 'BOSS'`, matching `item.Name`, over `Storage?$expand=*($levels=1)`
  (ANSIBLE `roles/redfish_storage_volume/molecule/__extract_storage.yml`, and
  `.../apply_time_default_BOSS-11160/converge.yml`, which creates a `RAID1` "VD_BOSS" on it). `SetBootVdREDFISH.py`
  likewise tests `"BOSS" in Model or "S1" in Model` against `StorageControllers[].Model` (SCRIPTS, lines ~152-160).
- Other Id prefixes Dell shows: `RAID.Integrated.1-1`, `RAID.Slot.6-1`, `RAID.Mezzanine.1-1`, `RAID.SL.3-1`,
  `CPU.1`, `AHCI.Slot.1-1`, `AHCI.Embedded.{1,2}-1`, `PCIeSSD.Slot.2-C`, `PCIeExtender.Slot.N` (WP2018 section 3.1 and
  3.2 examples; SCRIPTS help text), `PCIeSSD.Integrated.1-C` (MOCK-10G-XE), `NonRAID.Slot.1-1` (HBA465e, MOCK-9G-16B).

### `Volumes` link location

`Storage/<Id>` carries `Volumes: {"@odata.id": ".../Storage/<Id>/Volumes"}` on every captured member of both
generations, including AHCI, BOSS, CPU.1, NonRAID, PERC. The Dell script walks it blindly:
`GET .../Storage/<member>/Volumes` then `data['Members']` (SCRIPTS `GetStorageInventoryREDFISH.py`, `get_virtualdisks`,
lines ~136-153). Dell's 2018 paper shows the link in a Storage resource of an older `Storage.v1_3_0` PERC H330 Mini
(WP2018 section 3.1). The Storage schema does **not** make it required (`required`: `@odata.id`, `@odata.type`, `Id`,
`Name`, from the Dell-served `Storage.v1_16_0.json`), so tolerating an absent link as "no volumes" (as `_controllers`
does) is correct per contract.

### Schema versions served

| Firmware | Storage | Volume |
|---|---|---|
| iDRAC9 7.10.30 / 7.10.50 | v1_15_0 | v1_9_0 |
| iDRAC9 7.20.10.05 | v1_16_0 | v1_10_0 |
| iDRAC10 1.10.17.00 | v1_16_0 | v1_10_0 |
| iDRAC10 1.20.70.50 | v1_18_0 | v1_10_1 |
| iDRAC10 1.30.07.10 | v1_19_0 | v1_10_2 |
| iDRAC9 3.x (June 2018 paper) | v1_3_0 | (VolumeType only) |

(Sources: `@odata.type` of the captured resources.) The same `RAIDType` enumeration is in both generations (below).

### Firmware floor for `RAIDType`

- iDRAC9 **3.36.36.36** guide's Volume section does not list `RAIDType`, only `VolumeType` ("The type of the associated
  volume.") (GUIDE-3.36, "Storage - Settings resource" table; `grep RAIDType` finds nothing).
- iDRAC9 **4.20.20.20** guide lists "RAIDType | String | Yes | The RAID type of this volume." and
  "VolumeType | String | Yes | The type of this volume." (GUIDE-4.20, Volumes table, p. 371).
- Dell's own Create script posts `{"RAIDType": "RAID1", ...}` and its Ansible module maps legacy values
  `Mirrored -> RAID1, NonRedundant -> RAID0, StripedWithParity -> RAID5, SpannedMirrors -> RAID10,
  SpannedStripesWithParity -> RAID50` (ANSIBLE `redfish_storage_volume.py` lines 405-409), i.e. both vocabularies coexist.
- Exact firmware between 3.36 and 4.20 where `RAIDType` first appears: **UNVERIFIED** (Dell publishes no 4.00/4.10 guide
  I could reach; 4.00 release notes carry no mention).

### Expand behaviour

- Dell documents `Storage?$expand=*($levels=1)` and `Storage/<id>/Volumes?$expand=*($levels=1)` as the supported way to
  fetch controllers and their volumes (FAQ section 4.1, 4.2, Dec 2024 edition; Dell's Ansible role uses the former).
  `$expand` is also warned as slow: "Depending on the number of nested URIs $expand may take a few seconds ... On max
  server configurations $expand may take longer" (FAQ 42.3). `levels=1` on `Storage` expands the member objects but
  `Volumes` stays a link, so it saves the per-member GET only. Whether it is accepted on iDRAC9 < 6.x: **UNVERIFIED**.
- Our `get_collection` already tolerates both link-only and inlined members (`"@odata.type" in member`).

### Oem blocks

On both generations a Volume carries `Oem.Dell.DellVirtualDisk` **and** `Oem.Dell.DellVolume`; the former is flagged
`"DellVirtualDisk@Redfish.Deprecated": "Please migrate to use DellVolume"` (MOCK-10G, iDRAC10 1.10.17.00,
`Volumes/Disk.Virtual.1_RAID.SL.3-1/index.json`). `RaidStatus` is in both. Do not depend on the deprecated one.

## 2. Topologies

| Case | Evidence | Result |
|---|---|---|
| Two or more PERCs | Dell shows `RAID.Slot.6-1`, `RAID.Integrated.1-1`, `RAID.Mezzanine.1-1`, `RAID.SL.3-1` as separate members (WP2018, SCRIPTS). A dual-PERC platform exists ("dual PERC support on PowerEdge R750 with 24x NVMe configuration", RN-5.00.10). No capture with 2 PERCs. | Each is its own Storage member with its own Volumes; walk handles it. Multi-PERC capture itself: UNVERIFIED. |
| PERC + BOSS | MOCK-9G-16: `RAID.SL.5-1` + `BOSS.Integrated.1-1`; MOCK-9G-15: `RAID.SL.3-1` + `AHCI.SL.6-1` (BOSS-S2). | Separate members; BOSS RAID1 VD would be under the BOSS member (see section 3). |
| Front + rear PERC | Both are `RAID.*` members; naming of the rear (`RAID.SL.x`, `RAID.Slot.x`) is slot-dependent. No capture. | UNVERIFIED beyond "separate members". |
| HBA (adapter or mini) | HBA465e = `NonRAID.Slot.1-1`, `Name` "HBA465e Adapter", `Volumes` link present, `Members: []`, `Status.Health: null` (MOCK-9G-16B). Chassis enclosure for a PERC in non-RAID mode: `Enclosure.Internal.0-1:NonRAID.SL.3-1` (FAQ 4.7). | Prefix `NonRAID.`, not `RAID.`, so it falls to `OTHER` in `controller_kind`; zero volumes. HBA330 specifically (Mini/Adapter Id): **UNVERIFIED** (no capture; the `NonRAID.` prefix is the pattern by analogy only). |
| PERC with disks left non-RAID | R660xs `RAID.SL.5-1` (CurrentControllerMode `RAID`), 2 drives, `Volumes` collection `Members: []`, drive `Oem.Dell.DellPhysicalDisk.RaidStatus: "Ready"`, drive `Links.Volumes: []` (MOCK-9G-16). | Empty `Volumes`, not an error. |
| PERC in "HBA mode" | Dell: "Changing the controller mode is only supported on PERC 10 or older generation cards" via `ControllerMode: HBA` (FAQ 4.14). `DellController.CurrentControllerMode` schema text: "It has only one value and is RAID" (served `DellController.v1_5_0.json`, 7.20). | Pre-PERC11 only; resulting Storage Id/volume shape in HBA mode: **UNVERIFIED**. |
| S130/S140/S150/S160 software RAID | iDRAC lists these ("Support for PERC S150, software RAID controller on PowerEdge XE8545", RN-5.00.10). Known limitations: "On physical disks configured with software RAID, creating virtual disk is not supported in Redfish interface" (RN-4.20, tracking 153951); "iDRAC interfaces do not support slicing RAID volumes through software RAID controllers" (RN-5.00.10). 3.36 guide's example volume is `Disk.Direct.0-0:AHCI.Embedded.2-1` (GUIDE-3.36 p.170). | Id prefix of an S160 Storage member, and whether its RAID1 appears as a Volume with `RAIDType: RAID1`: **UNVERIFIED** (no capture or doc). Likely `AHCI.*`/embedded, by analogy to the 3.36 example and BOSS-S2, but that is inference. |
| Foreign configuration | Dell action `DellRaidService.ImportForeignConfig`/`ClearForeignConfig` exists (SCRIPTS PowerShell). `DellVirtualDisk.RaidStatus` enum includes `Foreign` (below). | Whether foreign VDs are listed in `Volumes` at all: **UNVERIFIED**. |
| NVMe-only | `CPU.1` member, one Volume per drive, `VolumeType: "RawDevice"`, `RAIDType: null` (MOCK-10G-AMD R7725, MOCK-10G-XE, MOCK-9G-16B). `Oem` is `{}` on CPU.1. | RAID1 count 0; no `Controllers`/`StorageControllers` on CPU.1 in MOCK-10G-XE. |
| BOSS present but no VD | MOCK-9G-16 BOSS-N1 Modular: `Volumes` `Members: []`, `Members@odata.count: 0`; yet two `RawDevice` Volume resources (`Disk.Direct.0-0:BOSS.Integrated.1-1`) exist on disk in the crawl. MOCK-9G-15 BOSS-S2 and MOCK-9G-16B BOSS-N1: collection lists the two `Disk.Direct` volumes (count 2 for S2, 0 for N1, with the volume files still present). | A BOSS without a VD reports either no volumes or two `RawDevice` volumes with `RAIDType: null`, depending on firmware. Why the 7.20 / 7.10.50 collections are empty while the Volume files exist: **UNVERIFIED** (real behaviour vs crawl artifact). Either way: RAID1 count 0. |

## 3. RAIDType, VolumeType, degraded volumes

### Enumerations (served by the iDRAC, identical on iDRAC9 7.20 and iDRAC10 1.10.17)

`Schemas/Volume.json` served in MOCK-9G-16 and MOCK-10G, and DMTF `swordfish/v1/Volume.json` (identical):

- `RAIDType`: `RAID0, RAID1, RAID3, RAID4, RAID5, RAID6, RAID10, RAID01, RAID6TP, RAID1E, RAID50, RAID60, RAID00,
  RAID10E, RAID1Triple, RAID10Triple, None`. `None` has `enumVersionAdded: v1_4_2`. The property is
  `anyOf [RAIDType, null]`, `versionAdded v1_3_1`.
- `VolumeType`: `RawDevice, NonRedundant, Mirrored, StripedWithParity, SpannedMirrors, SpannedStripesWithParity`; marked
  "Deprecated in favor of explicit use of RAIDType."

### Values Dell actually emitted in the captures

- PERC H965i (iDRAC10 1.10.17.00): `RAID0`/`NonRedundant` x4 and `RAID1`/`Mirrored` x1 (MOCK-10G, `Disk.Virtual.4:RAID.SL.3-1`).
- PERC H745 (7.10.30): `RAID0`/`NonRedundant` (MOCK-9G-15).
- BOSS and NVMe direct volumes: `RAIDType: null`, `VolumeType: "RawDevice"`, `MediaSpanCount: null`,
  `Encrypted: null`, `DisplayName: null` (MOCK-9G-16 `Disk.Direct.0-0:BOSS.Integrated.1-1`).
- Dell's Create script accepts RAID levels `0,1,5,6,10,50,60` and sends them as `RAIDx` strings
  (SCRIPTS `CreateVirtualDiskREDFISH.py` line ~270); a PERC controller advertises `SupportedRAIDTypes`
  `[RAID0, RAID1, RAID5, RAID6, RAID10, RAID50, RAID60]`, a BOSS-N1 `[RAID0, RAID1]`, a BOSS-S2 `[RAID1]`.
- No capture shows RAID5/6/10/50/60/1E/Triple, or the literal string `"None"`. The string `"None"` is *allowed* by the
  schema, so a defensive `str(...).upper() == "RAID1"` equality is right and `"None"` must not be read as RAID1.
- Trap: `Storage.StorageControllers[0].SupportedRAIDTypes` / `Controllers/<id>.SupportedRAIDTypes` is a **capability**
  list, containing `RAID1` on every BOSS and PERC. Never use it as evidence of a configured mirror.

### Degraded / failed / foreign

- Volume `Status.Health`/`HealthRollup`/`State` are the standard Redfish triple (all `OK`/`Enabled` in captures).
- `Oem.Dell.DellVirtualDisk.RaidStatus` enum (served `DellVirtualDisk.json`, iDRAC10 1.10.17.00):
  `Blocked, Degraded, Failed, Foreign, Offline, Online, Ready, Unknown`; `PrimaryStatus`: `Degraded, Error, OK, Unknown`.
  `RemainingRedundancy` (int|null) and `OperationName` (e.g. "None") also exist.
- Whether a degraded or failed RAID1 VD **remains in the `Volumes` collection**: the existence of `Degraded`/`Failed`/
  `Offline` states on a volume object implies it does, but no capture or Dell text states it. **UNVERIFIED.** Assume yes
  but do not depend on `Health == OK`: `RAIDType` is a configuration fact, health is separate (and our code already
  carries `health`).

## 4. Paging, laziness, count, absent links

- No captured `Storage` or `Volumes` collection carries `Members@odata.nextLink` (a recursive grep of all six mockups
  finds it only in the LC log `Entries`, `SerialInterfaceCollection`/`NVMeDomainCollection` schema text). Dell's FAQ
  documents `$top`, `$select`, `$expand`, `$filter` for iDRAC9/10 (FAQ section 42) but says nothing about paging
  Storage. `get_collection` following `nextLink` is therefore harmless and correct.
- `Members@odata.count` equals `len(Members)` in every capture. Dell's Ansible module trusts the count
  (`Members@odata.count == 0` / `0 < count`, ANSIBLE `redfish_storage_volume.py` ~lines 658-661) and Dell's script trusts
  `Members == []` (`GetStorageInventoryREDFISH.py` line ~143). Our `get_collection` ignoring the count and reading
  `Members` is the safer choice and consistent.
- Lazy population: Dell does not document volumes being populated lazily. The only indication of an inventory lag is
  Dell's `CSIOR` ("Collect System Inventory On Restart") gating hardware inventory, mentioned in RN-5.00.10 for NVMe
  ("ensure that CSIOR is enabled"); a stale `DellController.LastSystemInventoryTime`/`LastUpdateTime` is exposed
  (`2025-02-21T23:43:07+00:00` in MOCK-10G). Whether `Volumes` can be empty merely because inventory has not run:
  **UNVERIFIED**.
- Absent or 404 `Volumes`: not seen. Every captured member (CPU.1, AHCI, BOSS, NonRAID, PERC, PCIeSSD) has the link and
  the collection GET succeeds with `Members: []`. Dell's script has no guard and would raise `KeyError` on an error body,
  which implies Dell never expects it to fail. Contractually it is optional (section 1). The GPU/DPU mockup contains a
  directory `Storage/RAID.Slot.1-1` whose body is an `error` (SYS403, resource not found) and `RAID.Backplane.Firmware.1`
  (a directory, not a member); neither is a listed member (collection: `CPU.1, NonRAID.Slot.1-1, AHCI.Embedded.2-1,
  AHCI.Embedded.1-1, BOSS.SL.16-1`) so these look like crawl residue, not a firmware behaviour.
- `Volume.Id` is `Disk.Virtual.4:RAID.SL.3-1` (colon) while its `@odata.id` ends `Disk.Virtual.4_RAID.SL.3-1`
  (underscore) on 7.10+/10 (MOCK-10G). `volume_to_dict` stores `Id`; fine as an opaque label, but do not
  string-compare it with the `@odata.id` tail. Action `target`s even use a third form (`Disk.Virtual.1:RAID.SL.3-1`
  inside a `_`-style path), so treat all as display only.

## 5. Is a RAID1 OS volume distinguishable from a RAID1 data volume?

No reliable per-volume indicator exists in what Dell serves:

1. **Volume properties.** The Dell-served `Volume.v1_10_0` property list (iDRAC10 1.10.17.00 and iDRAC9 7.20.10.05) is:
   `BlockSizeBytes, CapacityBytes, Description, DisplayName, Encrypted, EncryptionTypes, Id, Identifiers, Links,
   MediaSpanCount, Name, Oem, Operations, OptimumIOSizeBytes, RAIDType, ReadCachePolicy, Status, VolumeType,
   WriteCachePolicy`. There is no `BootVolumeSource`; DMTF's own `Volume.v1_10_0` adds `IsBootCapable` ("whether or not
   the Volume contains a boot image and is capable of booting", `versionAdded v1_7_0`, read-write) and `VolumeUsage`,
   but Dell does not serve either. `Links` holds only `Drives`.
   `DellVolume` fields: `BusProtocol, Cachecade, DiskCachePolicy, LastSystemInventoryTime, LastUpdateTime, LockStatus,
   MediaType, ObjectStatus, OperationName, OperationPercentComplete, PrimaryStatus, RaidStatus, RemainingRedundancy,
   SpanDepth, SpanLength, StartingLBAinBlocks, StripeSize, T10PIStatus, VirtualDiskTargetID`. None marks boot/OS use.
   `Encrypted` is a security setting, not a role.
2. **`Oem.Dell.DellController.BootVirtualDiskFQDD`** on the Storage member. Dell's description: "This property indicates
   FQDD of BootVD. If no VD is set as BootVD in the controller, this property is an empty string." (served
   `DellController.v1_5_0.json`.) Dell reads it with `Storage/<id>?$select=Oem/Dell/DellController/BootVirtualDiskFQDD`
   and sets it with `DellRaidService.SetBootVD` (SCRIPTS `SetBootVdREDFISH.py`). In the captures: `null` on R770 H965
   (all five VDs), `null` on R660xs H965 and on both BOSS cards, `"Disk.Virtual.0:RAID.SL.3-1"` on the R650 PERC H745
   whose only VD is RAID0 (MOCK-9G-15). Observed value for "unset" is JSON `null`, not the documented empty string.
   It is a controller BIOS/boot-order setting, populated by whoever ran SetBootVD or the BIOS; a UEFI install on a VD
   that was never marked bootable leaves it null, so a null is not evidence of "no OS volume". It is **absent** (not even
   an `Oem` key) on `CPU.1`, and it is `null` on BOSS (not supported there).
3. **System boot options.** `Systems/System.Embedded.1/BootOptions/BootXXXX` have `DisplayName`s like
   "RAID Controller in SL 3: Windows Boot Manager" (R770, MOCK-10G), "AHCI Controller in SL 6: VMware ESXi" (BOSS-S2 on
   MOCK-9G-15), "Unavailable: Ubuntu" (an entry whose device is not present), and `UefiDevicePath`
   `HD(1,GPT,...)/\EFI\Microsoft\Boot\bootmgfw.efi`. This is the only place that says an OS boot loader exists on a given
   controller. It names the controller by slot text, not by the Storage Id, and is a BIOS/UEFI NVRAM list (stale
   entries remain, hence "Unavailable:"). It cannot tell RAID1 from a single disk.

Recommendation on using a boot indicator: **do not make the rule depend on any of these.** `BootVirtualDiskFQDD` is null
in most captures and says nothing on BOSS; boot entries are slot text. If more precision is wanted later, treat
`BootVirtualDiskFQDD` as an additive signal only: "if set, and that VD is not RAID1/redundant, fire". Keep the base rule
as "some RAID1 (or other redundancy) exists on a BOSS or PERC".

## 6. Recommended corrections (concrete findings)

1. **`controller_kind`: classify by `Name`/`Model` first, Id prefix second.** Add: `Name` or
   `StorageControllers[0].Model` containing `BOSS` -> `BOSS` (covers `AHCI.SL.6-1` BOSS-S2, `BOSS.Integrated.1-1`,
   `BOSS.SL.16-1`); keep `BOSS.` prefix; add `NonRAID.` -> a new `HBA` kind (or `OTHER`, which it already reaches);
   `PCIeSSD.`/`PCIeExtender.` -> `NVME`; keep `AHCI.Embedded.` as plain SATA. This is label-only today, but
   the "BOSS card whose M.2 drives were never made into a mirror" text in the policy needs `kind == BOSS` to be true
   for BOSS-S2 to be able to say it. `Name` as the discriminator is what Dell's own tests do.
2. **Fallback for old `RAIDType`-less firmware.** `volume_to_dict` reads only `RAIDType`. On iDRAC9 3.x (no `RAIDType`;
   guide 3.36) a mirror is `VolumeType == "Mirrored"`. If `RAIDType` is null and `VolumeType == "Mirrored"`, derive
   `RAID1`. Caveats: Dell's Ansible module maps `Mirrored` to `RAID1` (ANSIBLE line 406); on newer firmware `Mirrored`
   could in principle also describe `RAID1E`/`RAID1Triple` (Dell never emitted either in any capture); and is it worth it
   at all if the estate is 14G+ on iDRAC9 4.20+? Operator call.
3. **Never use `SupportedRAIDTypes` or a `RawDevice` volume count.** BOSS and NVMe produce `RawDevice` volumes with
   `RAIDType: null`; a naive "volume_count > 0 means has RAID" would be wrong. `raid.volume_count` currently counts them
   (it feeds the evidence string "N virtual disk(s)"): consider counting only volumes with a non-null `RAIDType` as
   "virtual disks".
4. **Scope of `raid.os_mirror_missing`.** It fires CRITICAL on any server with `controllers` non-empty and no RAID1. From
   the captures that includes: NVMe-only servers (`CPU.1` only), servers with only an HBA (`NonRAID.*`), PERC with disks
   left non-RAID (volumes empty), servers using OS-level RAID/SAN boot, and servers where software RAID (S160) is the only
   mirror and may not be visible in Redfish. Recommend: only evaluate when a RAID-capable controller (kind `RAID` or
   `BOSS`) exists; otherwise treat as "no data" (skip), not CRITICAL. A BOSS or PERC present with zero RAID1 stays a real
   finding.
5. **Data RAID1 vs OS RAID1.** The rule passes if any RAID1 exists anywhere, including a RAID1 data VD on a PERC while
   the OS sits on an unmirrored BOSS or single disk. Dell exposes no flag to separate them (section 5), so this
   false-negative is inherent. Best-available mitigation if wanted: require the RAID1 on a `BOSS` controller when a BOSS
   is present (a BOSS exists to hold the OS), else any PERC RAID1. Operator call.
6. **Other redundant OS layouts.** A RAID10/RAID5/RAID6 VD on a PERC can carry the OS redundantly (RAID1E/RAID10 for
   2-4 disks). The rule name says RAID1; decide whether `VolumeType in {Mirrored, SpannedMirrors, StripedWithParity,
   SpannedStripesWithParity}` / `RAIDType` other than `RAID0`/null should also satisfy it. Not a Dell question.
7. **Do not trust health to infer existence.** A degraded or failed RAID1 still has `RAIDType: RAID1` and a `Status.Health`
   of Warning/Critical, and Dell exposes `Oem.Dell.DellVolume.RaidStatus` (`Degraded`, `Failed`, `Offline`, `Foreign`).
   Do not drop a volume with non-OK health from the RAID1 count; if the VD vanishes in some failure mode the rule would
   rightly fire. (Presence in the collection after a failure is UNVERIFIED.)
8. **Failure handling is already right.** Absent `Volumes` link -> `[]` (contractually optional); a failed `Volumes` GET ->
   whole `controllers` is `None` (carry forward). Keep both. `get_collection` ignoring `Members@odata.count` is correct.
9. **Optional efficiency.** `GET Storage?$expand=*($levels=1)` returns every controller in one call (Dell's documented
   and Ansible-used path; FAQ 4.1) and `Volumes?$expand=*($levels=1)` returns every volume (FAQ 4.2). Not needed for
   correctness at 2.5k-5k servers; note Dell warns it can be slow on large configurations (FAQ 42.3).
10. **Fake provider/doc sync (repo convention 10 and 11).** The fake generator should emit at least: a PERC with RAID1,
    a BOSS with no RAID1 (RawDevice volumes, null RAIDType), a BOSS-S2 under `AHCI.SL.x`, an NVMe `CPU.1`-only server, and
    an HBA `NonRAID.*` member, so the policy-scope change above is exercised. The facts in this note belong in
    `docs/dell-collectors.md` (which today contains no storage-volume section; a `grep` for RAID/volume/BOSS finds
    only an unrelated UCS `serverStorage` line).

## Open questions / UNVERIFIED

| # | Unsettled | What would settle it |
|---|---|---|
| 1 | Current Dell iDRAC9 7.xx and iDRAC10 API *reference* text (developer.dell.com is an SPA; `openapi.yaml` returns the HTML shell) | Read https://developer.dell.com/apis/2978 (iDRAC9) and the iDRAC10 entry in a browser, or have the operator export the iDRAC's own `Schemas/` (already done here via mockups) |
| 2 | BOSS-N1 Storage Id and `Name` on iDRAC10 / 17G (R670/R770) | One `GET /redfish/v1/Systems/System.Embedded.1/Storage` on a 17G BOSS-bearing host (or a Dell iDRAC10 mockup with a BOSS) |
| 3 | A real BOSS **with a configured RAID1 VD**: member Id of the volume, `RAIDType`, `VolumeType`, whether the `RawDevice` pair disappears. Every published BOSS capture is unconfigured | `GET .../Storage/<BOSS>/Volumes?$expand=*($levels=1)` on a host with a BOSS mirror (the operator's estate will have many) |
| 4 | S130/S140/S150/S160 software RAID: Storage Id prefix, and whether a software RAID1 is a Volume with `RAIDType: RAID1` | Same GET on an S160 host; BIOS SATA mode must be RAID |
| 5 | HBA330 (Mini/Adapter) Id prefix | GET on an HBA330 host (expected `NonRAID.*` by analogy to HBA465e, not verified) |
| 6 | Foreign configuration: are foreign VDs listed in `Volumes`, with which `RaidStatus` | GET on a PERC with a foreign config, before import |
| 7 | A Degraded/Failed/Offline RAID1 VD: does it stay listed (inferred yes) and what are `Status.Health` and `RaidStatus` | Observe a host with a degraded VD (the estate's alerting will list one) |
| 8 | Why the 7.20 / 7.10.50 BOSS-N1 captures show an empty `Volumes` collection while `Disk.Direct.*` Volume files exist (firmware behaviour vs crawl artifact) | Live GET on a BOSS-N1 host, with and without a VD |
| 9 | Firmware where `RAIDType` first appeared (between 3.36 and 4.20) | A Dell 4.00/4.10 Redfish guide, or the Volume schema served by a 4.0x/4.1x iDRAC |
| 10 | Does `Storage?$expand=*($levels=1)` work on iDRAC9 older than 6.x | Test against the oldest iDRAC9 in the estate |
| 11 | Whether `Volumes` can be empty/stale merely because Lifecycle Controller inventory (CSIOR) has not run | Compare `DellController.LastSystemInventoryTime` with a fresh VD creation on a lab host |
| 12 | Multi-PERC (`RAID.Slot.1-1` + `RAID.Slot.2-1`, front + rear) naming on 17G | A GET on a dual-PERC host |
| 13 | `DellController.BootVirtualDiskFQDD` unset value: schema says empty string, captures show `null`; and how often it is populated in UEFI installs | Sample it across the fleet (cheap `$select`) before building anything on it |
| 14 | PERC "HBA mode" resulting Storage Id/volumes (only PERC10-and-older supports the switch per FAQ 4.14) | GET on a PERC10-class host switched to HBA |
