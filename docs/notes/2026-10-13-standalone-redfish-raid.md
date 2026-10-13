# Can the generic standalone Redfish collector tell whether a server has a RAID 1 for the OS?

Researched 2026-10-13. Primary sources only (HPE server management portal, HPE developer blog,
NVIDIA DGX docs, DMTF schema, Lenovo/Supermicro vendor docs, OpenBMC source). No live BMC was
queried; everything below is documentation, so "what the BMC really returns" stays UNVERIFIED
unless a field pass confirms it.

## Summary

Our path (`Systems/<id>/Storage` -> each member's `Volumes` -> `Volume.RAIDType == RAID1`,
`provider.py::_controllers`, `mapping.py::controllers_from_storage`) is the DMTF-correct way and is
sufficient **only where a hardware RAID controller (or a hardware boot-mirror device) publishes its
virtual disks as Redfish Volumes**. For HPE iLO 5 fw >= 2.30 / iLO 6 with Smart Array SR/MR and
NS204i it is plausibly sufficient (RDE devices return DMTF `RAID1`), but older iLO 5 firmware (< 2.30,
and SR before the RDE path) holds that data only in the OEM `SmartStorage` tree with a different
vocabulary (`"Raid": "1"`), and a Smart Array in HBA mode or a software/fake-RAID SATA controller has no
Volume at all. For NVIDIA DGX A100/H100/H200/B200 the OS mirror is Linux software RAID (mdadm) inside the
host OS, invisible to the BMC; NVIDIA's BMC docs say only "Get storage Inventory". **A Redfish-only
collector cannot determine OS-mirror state on DGX**, so "no RAID1 volume" there must be UNKNOWN, never
CRITICAL. Two code observations are in "Implications for the code" below (the Dell-only `raid_missing`
policy is scoped to OPENMANAGE so the standalone path is not yet affected, but `raid.has_data` is true
for any non-empty controller list).

## 0. The Redfish contract

- DMTF `Volume` (Swordfish `Volume.json`, fetched 2026-10-13 from
  https://redfish.dmtf.org/schemas/v1/Volume.json, `$id` swordfish/v1/Volume.json): `RAIDType` enum is
  `RAID0, RAID1, RAID3, RAID4, RAID5, RAID6, RAID10, RAID01, RAID6TP, RAID1E, RAID50, RAID60, RAID00,
  RAID10E, RAID1Triple, RAID10Triple, None`. Exact case `RAID1`. In `Volume.v1_10_0.json`:
  `RAIDType` is `anyOf [enum, null]`, `"versionAdded": "v1_3_1"`, "This property shall contain the RAID type
  of the associated Volume." So it is nullable and absent on any Volume older than v1_3_1.
- `VolumeType` (`RawDevice, NonRedundant, Mirrored, StripedWithParity, SpannedMirrors,
  SpannedStripesWithParity`) is `"deprecated": "Deprecated in favor of explicit use of RAIDType."`
  (same v1_10_0 file). A BMC on an older schema may still send only `VolumeType: "Mirrored"` with no
  `RAIDType`. `volume_to_dict` already keeps `volume_type`, so a fallback is possible (see rule).
- Redfish has no property for host-OS software RAID. Nothing in Storage/Volume/Drive describes `md`
  devices; a BMC can only report what it sees on a bus or what a controller firmware publishes.

## 1. HPE

### 1.1 iLO 5

- Two parallel trees. DMTF `Systems/1/Storage` and the legacy OEM `SmartStorage`. HPE: "The OEM HPE legacy
  Smart Storage model used by iLO 4 and supported by iLO 5 is deprecated with iLO 6 and later."
  Source: https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/supplementdocuments/storage/
  ("Storage data models"; same text mirrored at redfish.redoc.ly).
- Minimum firmware for the DMTF tree: "Starting at Gen10 and Gen10 Plus (firmware 2.30+), HPE servers can
  take advantage of it", and on a firmware "below 2.30, the Storage DMTF URI is not populated. Only the
  SmartStorage and SmartStorageConfigX locations are populated."
  Source: https://developer.hpe.com/blog/storage-management-with-redfish (HPE developer blog; the 2.30
  figure is in an author's note, not in a firmware changelog -> UNVERIFIED as an exact boundary).
- PLDM for RDE: "started in iLO 5 firmware version 2.33 and in iLO 6 version 1.05"; "Redfish clients
  connected to an iLO 5 version 2.65 can perform read and write operations on logical drives (LD) hosted by
  HPE SR Smart Arrays running firmware version 5.00 and above".
  Source: https://servermanagementportal.ext.hpe.com/docs/references_and_material/blogposts/pldm/pldm_rde/pldm_rde.md
  (HPE portal blog, section "HPE implementation of PLDM for RDE"). So on iLO 5, SR logical drives appear as
  Volumes only with iLO 5 >= ~2.65 AND Smart Array firmware >= 5.00. That pair is the practical minimum.
  The RDE URI table says `.../Storage/{id}/Volumes` GET is supported on "5, 6" (iLO generations):
  https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/supplementdocuments/rdesupport
  ("iLO RDE URIs and corresponding HTTP methods"; first firmware per operation is "in the iLO 5 changelog",
  not read -> UNVERIFIED per operation).
- Member Ids: three classes with a hash-ID prefix: "`/DExxxxxx`" PLDM-for-RDE devices (SR/MR Smart Array,
  NS204i), "`/DCxxxxxx`" DCi devices (some FC HBAs, NICs), "`/DAxxxxxx`" "Non RDE, non DCi capable devices:
  ... not configurable and use the SMBIOS protocol ... Direct attached NVMe drives, and their embedded
  controller" (pldm_rde.md, "Device naming convention"). Documented examples: `DE040000`
  (HPE MR216i-o Gen11), `DE042000` (HPE NS204i-u Gen11 Boot Controller), `DA000005`, `DA000006`
  (storage page above, "GET requests"); `DE07C000` (SR, pldm_rde.md); `DE00B000` (community thread, not
  primary). The task's `DE00A000` is not in any primary source I found. HPE warns "Hash IDs may change
  over time." Conclusion: Ids are opaque; **our Dell prefix table (`RAID.`/`BOSS.`/`AHCI.`/`CPU.`) matches
  none of them, so every HPE member is `kind: "OTHER"`** (`controller_kind`, mapping.py:269-284).
  Controller type has to come from `Storage.Name` / `StorageControllers[].Model` / `SupportedRAIDTypes`,
  not from the Id.
- Who answers: "Redfish client requests sent to `/DA` and `/DC` devices are answered by the iLO. ... Requests
  sent to RDE capable devices (`/DE`) are answered by the device, usually not manufactured by HPE. The
  schema version implemented in the device may be different" and OEM blocks are "Oem/<manufacturer>", not
  Oem/Hpe (pldm_rde.md, "Consequences of intra-server multi protocol coexistence"; rdesupport page: "they
  don't contain any HPE Oem/Hpe section"). So Volume property shapes vary per controller maker.
- iLO 5 reference for the iLO-answered (DA) Storage/Volume: "Storage resource definitions of iLO 5 v3.09",
  https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo5/ilo5_309/ilo5_storage_resourcedefns309
  - `Volume` `@odata.type: "#Volume.v1_0_0.Volume"`, URI `/redfish/v1/systems/{item}/storage/{item}/volumes/{item}`;
    the only documented property set is CapacityBytes, DedicatedSpareDrives, Status, VolumeType, and
    VolumeType's only listed value is `RawDevice` ("The volume is a raw physical device without any RAID or
    other virtualization applied"). **No `RAIDType` property is documented on the iLO-answered Volume in
    iLO 5 v3.09.** `SupportedRAIDTypes` (RAID0, RAID1, RAID10, RAID1Triple, ...) is documented on Storage/
    StorageController only. That is a documented disagreement with the RDE blog (device-answered Volumes do
    carry `"RAIDType": "RAID1"`, pldm_rde.md create-volume body) and is why the schema is "per device".
  - The page gives no Id examples and no firmware introduction version.
- RAIDType strings on HPE DMTF volumes: device-supplied, DMTF spelling. HPE's create-volume example posts
  `"RAIDType": "RAID1"` (pldm_rde.md "DE07C000/Volumes"), and its MR capabilities example lists "None, RAID0,
  RAID1, RAID10, RAID5, RAID50, RAID6, RAID60" (storage page). A GET response body showing RAIDType was not
  retrievable from the portal (code blocks are rendered client-side); a community post cites
  `"RAIDType": "RAID1"` on `DE00B000` (https://community.hpe.com/t5/server-management-remote-server/ilorest-error-when-creating-a-raid/m-p/7204511 via search
  result; secondary, not fetched). **`Raid1`, `Raid1ADM`, `Raid10` (camel-case) are the OEM SmartStorageConfig / ilorest
  spellings, not the DMTF Volume ones** (ilorest `customdrive` doc: "raid-level: Raid0, Raid1, Raid1ADM, Raid10,
  Raid10ADM, Raid5, Raid50, Raid6, Raid60", https://servermanagementportal.ext.hpe.com/docs/redfishclients/ilorest-userguide/smartarraycommands, seen via search snippet only).
  A three-way mirror is `RAID1Triple` in DMTF (enum above), not `RAID1`.
- OEM tree (the only source on iLO 4 and iLO 5 < 2.30, and what a Gen10 SR reports when not on the RDE
  path): `/redfish/v1/systems/{item}/smartstorage/arraycontrollers/{item}/logicaldrives/{item}`, type
  `#HpeSmartStorageLogicalDrive.v2_3_0`, property `Raid` = "The current RAID level configured on the logical
  drive", values `0`, `1` ("Two sets of duplicate data is being stored on a pair of drives"), `1ADM`, `10`,
  `10ADM`, `5`, `50`, `6`, `60` (strings without the word RAID).
  Source: iLO 5 v3.09 OEM page,
  https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo5/ilo5_309/ilo5_hpe_resourcedefns309
  ("HpeSmartStorageLogicalDrive", property Raid). The HPE blog calls the older write path `"Raid": "Raid1"`
  (SmartStorageConfig PUT); read vocabulary (`"1"`) and write vocabulary (`Raid1`) differ -> do not reuse one
  parser. Where the SmartStorage link hangs on the ComputerSystem (Oem.Hpe.Links.SmartStorage) was not
  verified in this pass -> UNVERIFIED.
  Gen10 SR is dual-modelled: "iLO 5 based servers (Gen10 and Gen10 Plus) offer the possibility to manage
  those devices using both SmartStorageConfig and Storage data types" (pldm_rde.md Notes). The OEM model
  covers only SR; "removed in Gen11" (blog), "deprecated with iLO 6".
- Gen10 Smart Array in HBA mode / embedded SATA (S100i-style software RAID): no source in this pass shows a
  Volume representing it. HBA mode by definition exposes raw drives; with no logical drive there is nothing
  to report. Whether the embedded SATA controller appears as a `DA` Storage with `RawDevice` Volumes: not
  documented -> UNVERIFIED (a live iLO 5 would settle it).
- Volumes link/expand: documented as `Storage.Volumes` -> collection at `.../storage/{item}/volumes` (GET,
  POST). Nothing about `$expand` on these collections in the pages read -> UNVERIFIED. (Our code does
  `get_collection` on the link, which is the safe form.) Note `Links.Volumes` also exists on Drive, and the
  Storage example in HPE's page shows `Volumes@odata.count: 1` plus a `Volumes` array of links for the
  Drive, not the Storage; do not confuse them (storage page, "Volume properties").
- NS204i (M.2 boot pair, hardware mirror): modelled as an RDE device (`DE042000`, "HPE NS204i-u Gen11 Boot
  Controller", listed by `ilorest storagecontroller`) with one Volume (`/Volumes/239` in HPE's example) and
  its drives at `.../Storage/{NS204Id}/Drives` ("NS204 drives are not listed in the DriveCollection schema.
  Their URIs are under /redfish/v1/Sytems/1/Storage/{NS204Id}/Drives, which is not in the DMTF's
  specification" - a quote with HPE's own typo). Volume deletion is unsupported ("The volume URI of NS204
  storage devices does not allow DELETE requests", use `ResetToDefaults`), i.e. the NS204i always carries its
  mirror volume until reset. Source: storage page above, "Drive secure erase". The NS204i-p variant and
  any Gen10 availability: not in primary sources read. The community NS204i monitoring thread
  (https://community.hpe.com/t5/proliant-servers-ml-dl-sl/monitoring-ns204i-gen11-raid-status-with-ilorest/td-p/7199210) returned 403 and was not read. RAIDType of the NS204i volume: UNVERIFIED (the device
  supplies it; expected `RAID1`).
- iLO 6: DMTF Storage is "the only implemented type" (pldm_rde.md Notes); RDE since iLO 6 1.05
  (rdesupport page). Same DE/DA Ids and device-supplied RAIDType.

## 2. NVIDIA DGX

- Mirroring is software on every current model:
  - DGX A100: "Storage (OS) 1.92 TB NVMe M.2 SSD (ea) in RAID 1 array" (intro table,
    https://docs.nvidia.com/dgx/dgxa100-user-guide/introduction-to-dgxa100.html); service manual: boot drives
    `nvme0n1`/`nvme1n1`, `sudo mdadm -D /dev/md0` shows both "active sync", rebuild via
    `nvsm start /systems/localhost/storage/volumes/md0/rebuild`
    (https://docs.nvidia.com/dgx/dgxa100-service-manual/m2-nvme-replacement.html, "Identifying the Failed M.2
    NVMe", "Rebuilding the Boot Drive RAID 1 Volume").
  - DGX H100/H200: "Storage (OS) 2 x 1.92 TB NVMe M.2 SSD (ea) in RAID 1 array"
    (https://docs.nvidia.com/dgx/dgxh100-user-guide/introduction-to-dgxh100.html); service manual uses
    `mdadm --manage /dev/md0 --fail` (https://docs.nvidia.com/dgx/dgxh100-service-manual/m2-nvme-replacement.html).
    The DGX H200 intro URL I guessed 404'd; H200 is covered by the combined "DGX H100/H200" docs.
  - DGX B200: same table row; "software RAID begins rebuilding the RAID 1 array ... `nvsm show volumes` ...
    /systems/localhost/storage/volumes/md0/rebuild" (DGX B200 User Guide PDF, §3.2 Post Setup Tasks, build
    "Sep 08, 2026", https://docs.nvidia.com/dgx/dgxb200-user-guide/dgxb200-user-guide.pdf).
  - DGX-2: "The DGX A100 and DGX-2 systems embed two system drives for mirroring the OS partitions (RAID-1).
    If you want to enable mirroring, you need to enable it during the drive configuration of the Ubuntu
    installation. It cannot be enabled after the installation." (DGX Software Stack Installation Guide, via
    search snippet; page https://docs.nvidia.com/dgx/dgx-software-stack-installation-guide/ was fetched but the
    quoted sentence was not located in the extracted text -> treat as secondary). Notably **mirroring is
    optional at install time**, so "no RAID 1" can be a legitimate state.
  - DGX-1 (legacy): "plugged into the LSI controller (hardware RAID)"; "either a single-drive RAID-0 or a
    dual-drive RAID-1 array for the OS" managed by StorCLI. The only hardware-RAID DGX in the sources
    (best-practices storage page, https://docs.nvidia.com/dgx/bp-dgx/storage.html, "DGX Internal Storage").
    DGX-2 data drives: "managed by the OS using mdadm (software RAID)". No DGX A100/H100/H200/B200 page in
    this pass lists a hardware RAID card option: UNVERIFIED as a negative (absence of evidence).
- What the BMC exposes: NVIDIA's BMC docs list only "Get storage Inventory" among supported Redfish features,
  beside "Get PCIe device and functions inventory" (DGX H100/H200:
  https://docs.nvidia.com/dgx/dgxh100-user-guide/redfish-api-supp.html; DGX A100:
  https://docs.nvidia.com/dgx/dgxa100-user-guide/redfish-api-supp.html; DGX B200:
  https://docs.nvidia.com/dgx/dgxb200-user-guide/redfish-api-supp.html; B300 same). The server "follows the
  DSP0266 1.7.0 Specification and Redfish Schema 2019.1" (same pages; whether that bundle's Volume schema includes
  `RAIDType`, added in Volume v1_3_1, was not checked -> UNVERIFIED).
  The H100/H200 BMC page also lists "FRU Information ... PCIE Function, and Storage" as web-UI inventory
  (https://docs.nvidia.com/dgx/dgxh100-user-guide/bmc.html). Nothing in any NVIDIA DGX page documents
  `Volumes`, `StorageControllers` or `RAIDType` on the BMC, and the Redfish examples shown use
  `/redfish/v1/Systems/DGX` and AMI OEM blocks (`Oem.Ami`), i.e. an AMI MegaRAC stack (B300 page).
  Whether the BMC returns `Storage` members for the two M.2 drives or only the 8 U.2 drives: UNVERIFIED.
- Why software RAID is unreachable: the `md0` array lives in the host kernel; the BMC sees PCIe NVMe
  devices (the firmware changelog "Fixed where SEL logs might fill up for NVMe drives",
  https://docs.nvidia.com/dgx/dgxh100-fw-update-guide/firmware-changes.html, shows drives are BMC-visible).
  The only NVIDIA interface for md state is host-side `nvsm show volumes` / `mdadm`
  (`/systems/localhost/storage/volumes/md0`), which is NVSM's data model, not Redfish. No Redfish
  property reveals software RAID.
- OpenBMC alternative (some DGX-adjacent and generic ARM/OpenBMC boxes): bmcweb `storage.hpp` (master,
  fetched 2026-10-13 from
  https://raw.githubusercontent.com/openbmc/bmcweb/master/redfish-core/lib/storage.hpp) serves a single
  `Storage/1` with `Drives` only (lines ~100-125); the file contains zero occurrences of "Volume". Stock
  OpenBMC therefore never exposes Volumes/RAIDType. (DGX is documented as AMI-based, not stock bmcweb.)

## 3. Other standalone BMCs (one line each)

- Supermicro: exposes Storage with controller Ids like `HA-RAID`, `HBA`, `MRVL.HA-RAID`, `NVMeSSD` and
  volume URIs, for Broadcom 3108/3008/3216/3616/3908 and Marvell SE9230 (Redfish user guide 4.0,
  https://www.supermicro.com/manuals/other/redfish-user-guide-4-0/Content/general-content/raid-management.htm,
  "RAID Management"). The page's payloads use `"Raid": "RAID0"` / `"RaidLevel": "RAID1"` for creation and
  contain **no `RAIDType` property on a Volume GET** -> whether Volume GET carries `RAIDType` is UNVERIFIED
  (the PDF reference guide could not be fetched).
- Lenovo XCC / XCC2 / XCC3: Volumes under `Storage/<id>/Volumes/<n>` with documented `RAIDType`, example
  `"RAIDType": "RAID0"`; Storage Ids are slot-style (`RAID_Slot1`, `HBA_Slot10`, `7MM `, `M.2 `,
  `Direct_Attached_NVMe`, `Direct_Attached_SATA`, `VROC`; note trailing spaces in two Ids)
  (https://pubs.lenovo.com/xcc2-restapi/vol_managed_by_storage_controller_get and
  .../collection_storage_controller_get). The Lenovo example also carries an OEM `RaidLevel: "RAID 0"`
  (with a space) - do not parse that one. Intel VROC/software RAID appears as a `VROC` Storage member, so
  fake-RAID can be visible there (volume content for VROC: UNVERIFIED).
- Generic OpenBMC: no Volumes (above).

## 4. Conclusion and recommended rule

| Platform | `Storage -> Volumes -> RAIDType == RAID1` sufficient? |
|---|---|
| HPE iLO 6 (Gen11), SR/MR/NS204i | Yes, in principle: DE devices return DMTF `RAID1`. Needs member-Id-agnostic code (HPE Ids are never `RAID.`/`BOSS.`); also treat `RAID1Triple`, `RAID10`, `RAID1E` as redundant if the question is "OS is redundant". Field pass needed (RAIDType never seen in a primary GET body). |
| HPE iLO 5 fw >= 2.65 with SR fw >= 5.00 | Likely yes via RDE (same as above). |
| HPE iLO 5 fw 2.30 - <2.65, or SR < 5.00 | Storage exists but logical drives are likely absent from it: needs the OEM `SmartStorage/ArrayControllers/LogicalDrives[].Raid == "1"` path, or must be UNKNOWN. |
| HPE iLO 5 < 2.30, iLO 4 | DMTF Storage not populated; OEM path only; else UNKNOWN. |
| HPE HBA mode / embedded SATA software RAID | Cannot be determined (no Volume); UNKNOWN. |
| DGX A100/H100/H200/B200 (and DGX-2) | **No.** OS mirror is host mdadm; not in Redfish. UNKNOWN always. |
| DGX-1 | Hardware LSI RAID, but whether its BMC publishes Volumes is undocumented: UNKNOWN. |
| Supermicro | Volumes exist for HW RAID; RAIDType on GET unconfirmed: needs field check. |
| Lenovo XCC | Yes where a RAID/HBA/7MM controller is present; `RAIDType` documented. |
| Generic OpenBMC | No Volumes at all: UNKNOWN. |

Recommended rule for the standalone collector (no code written here):

1. Three states, not two: `MIRROR_PRESENT` / `NO_MIRROR` / `UNKNOWN`. `NO_MIRROR` (and therefore the CRITICAL
   "no RAID 1" policy) is allowed **only when positive evidence says a RAID-capable controller was
   read and had zero redundant volumes**. Positive evidence = at least one Storage member that publishes a
   non-empty `StorageControllers[].SupportedRAIDTypes` containing `RAID1` (or whose Volumes collection is
   present) and a successful, complete Volumes read. Everything else is UNKNOWN: no Storage, Storage with only
   Drives and no `Volumes` link, no SupportedRAIDTypes, BMC family known to hide software RAID (NVIDIA DGX,
   OpenBMC), HBA mode.
2. Treat `Volumes` link absent as "cannot tell", not as `[]`: the present `_controllers` code sets
   `volumes[key] = []` when the member has no `Volumes` link (provider.py:677-680), which then reads as
   "zero volumes" and, with `raid.has_data = bool(controllers)` (facts.py:174), would let a policy fire on a DGX
   whose BMC lists drives only. This is the concrete data-corruption risk found.
3. Redundant-mirror test: `RAIDType` normalised by upper-case, equal to one of `RAID1`, `RAID1E`,
   `RAID1TRIPLE`, `RAID10`, `RAID10E`, `RAID10TRIPLE`, `RAID01` (policy decision: is a RAID5/6 OS volume
   acceptable? the question as asked is "RAID 1", so keep RAID1 exact and add the others only by an explicit
   operator choice); fall back to `VolumeType == "Mirrored"`/`SpannedMirrors` when `RAIDType` is null/absent
   (older schema). Accept the HPE OEM spelling `"1"`/`"1ADM"` only from the OEM path, never from DMTF
   `RAIDType`.
4. Do not key on the Dell Id prefixes for non-Dell BMCs. For HPE use `Storage.Name`/`Model`; "OS" volume
   detection by boot flag is not available in these sources (no property found) -> cannot say the RAID 1 is
   the OS one, only that a mirror exists.
5. Scope the CRITICAL policy to collectors/vendors where it is verified (today OPENMANAGE) and, when extended
   to `REDFISH_STANDALONE`, scope by manufacturer (HPE, Lenovo) and gate on `raid.has_data` meaning "a Volume
   collection was read", not "a controller exists".

## Implications for the code (observations only)

- `controller_kind` (mapping.py:269) is Dell-FQDD-only; HPE/Lenovo/Supermicro members all map to `OTHER`.
- `raid.raid1_volume_count` (facts.py:176) compares `.upper() == "RAID1"`: tolerant of `Raid1`, but a
  3-way mirror (`RAID1Triple`) and `RAID1E` count as zero.
- `volume_to_dict` stores `raid_type = RAIDType or None`; a `None` is ambiguous between "older schema" and
  "RAID-less raw device" and `volume_type` is kept so the fallback is implementable.

## Open questions / UNVERIFIED

1. Exact iLO 5 firmware boundary for DMTF Storage population (blog says 2.30) and for Volumes on SR (2.65 +
   SR fw 5.00 per HPE blog). Settle: the iLO 5 changelog, look for "RDE support changes" (rdesupport page
   tells you to), or GET `Systems/1/Storage` on a Gen10 at known firmware.
2. Whether iLO-answered (DA) Volumes on iLO 5 carry `RAIDType` (v3.09 reference lists only `VolumeType:
   RawDevice`); how an embedded SATA controller and HBA-mode Smart Array look. Settle: live GET on a Gen10.
3. `RAIDType` literal in a real HPE Volume GET body (primary docs only show POST bodies and capabilities).
   Settle: a live DL360 Gen10+/Gen11 with SR and with NS204i; also confirm NS204i volume `RAIDType == "RAID1"`
   and NS204i-p Id/representation (the HPE community thread was 403).
4. Where `SmartStorage` hangs off the iLO 5 ComputerSystem (Oem.Hpe.Links) and whether SR logical drives
   appear in both trees simultaneously (HPE says dual management is possible).
5. `Volumes` `$expand` behaviour on iLO 5/6: not documented in the pages read.
6. DGX BMC: whether `Systems/*/Storage` members exist for the M.2 pair, whether any Volumes/StorageControllers
   appear, and which Redfish schema bundle ("2019.1") supports `RAIDType`. Settle: one live GET on a DGX
   H100 BMC (`/redfish/v1/Systems/DGX/Storage` and members), recorded as a fixture.
7. Hardware RAID option on DGX A100/H100/H200/B200: not found in any page read; DGX-2 mirroring being optional
   rests on a search snippet of the DGX Software Stack Installation Guide.
8. Supermicro Volume GET: is `RAIDType` present? (PDF guide not retrievable.) Lenovo VROC volumes: shown?
   Settle: live GET.
9. Scratch material (raw page text) was kept under the session scratchpad and is not committed.
