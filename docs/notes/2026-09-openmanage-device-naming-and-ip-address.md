# OME "Server Device Naming" and the real iDRAC IP address

Research only — no production code changed. This looks at a live collection
failure (`"unreachable, could not reach ocp4-compute-five-01"`) against
`app.infrastructure.providers.openmanage`, and asks what Dell's own OME REST
API guarantees about a managed device's *network address* versus its
*display name*, which OME's console-wide "Server Device Naming" setting
controls. The short answer, confirmed from Dell's own automation source
(the primary sources this environment could actually reach — see
"Sourcing note" below): **`TargetName` (Profiles) and `DeviceName`
(Devices) are both display names Dell's own scripts treat as
naming-policy-dependent, and Dell's own scripts never use either one to
reach a device — they resolve to `DeviceManagement[0].NetworkAddress`
instead, which every one of those scripts treats as the device's real
management IP regardless of naming policy.** This repo's collector reads
`TargetName`/`DeviceName` for the Redfish target address, which is exactly
the field Dell's own tooling avoids for that purpose.

## Sourcing note (read before trusting anything below)

This session could not get a live OME appliance, so both requested primary
sources — Dell's public Swagger/OpenAPI JSON at
`https://<OME-appliance>/api/$metadata` or `/openapi`, and Dell's REST API
guide PDF/HTML — were tried and mostly failed to return usable content:

- `https://dl.dell.com/topicspdf/dell-openmanage-enterprise_api-guide4_en-us.pdf`
  and `.../dell-openmanage-enterprise_reference-guide8_en-us.pdf` both
  returned HTTP 403/Access Denied to this environment's fetchers (Akamai
  edge block), so the schema tables in Dell's own REST API guide were
  **not directly readable this session**.
- The HTML mirrors of that same guide
  (`dell.com/support/manuals/.../ome_p_api_guide/...`,
  `.../lex_techrel_pub/device-service`) loaded, but this environment's page
  fetcher only ever returned the page's left-nav table of contents, never
  the body content (the guide is a client-rendered SPA) — confirmed by
  fetching the same page repeatedly with narrower prompts and getting the
  same nav-only result every time.
- `developer.dell.com/apis/...` (Dell's interactive API catalog, which is
  meant to be the modern replacement for the PDF guide) resolved but its
  fetched content was likewise limited to the introduction page, not a
  browsable schema.

What **did** work, and is what every finding below is actually cited to:
Dell's own official GitHub repositories of OME automation —
[`github.com/dell/OpenManage-Enterprise`](https://github.com/dell/OpenManage-Enterprise)
(Python/PowerShell sample scripts Dell publishes and maintains for OME) and
[`github.com/dell/dellemc-openmanage-ansible-modules`](https://github.com/dell/dellemc-openmanage-ansible-modules)
(Dell's own Ansible collection for OME, also official). Both are explicitly
in-scope per this task's source list ("Dell's own official code
samples/GitHub repos for OME automation"). Two Dell user-guide pages did
render past their nav shell and are cited directly where used. Everything
else is marked `UNVERIFIED` with what would settle it.

## 1. The "Server Device Naming" setting

**Confirmed to exist, name and values, from Dell's own Ansible module
source** — `ome_application_console_preferences.py`, the official
`dellemc.openmanage` collection:

- The module's `DOCUMENTATION` block, under `discovery_settings`:
  > `server_device_naming` — "Applicable to iDRACs only. C(IDRAC_HOSTNAME)
  > to use the iDRAC hostname. C(IDRAC_SYSTEM_HOSTNAME) to use the system
  > hostname." `choices: [IDRAC_HOSTNAME, IDRAC_SYSTEM_HOSTNAME]`,
  > `default: IDRAC_SYSTEM_HOSTNAME`.

  Source: [`plugins/modules/ome_application_console_preferences.py`, lines
  63–70](https://github.com/dell/dellemc-openmanage-ansible-modules/blob/collections/plugins/modules/ome_application_console_preferences.py)
  (fetched raw at
  `raw.githubusercontent.com/dell/dellemc-openmanage-ansible-modules/collections/plugins/modules/ome_application_console_preferences.py`).
  Mirrored in the rendered docs at
  [Ansible Community Documentation — `ome_application_console_preferences`](https://docs.ansible.com/projects/ansible/latest/collections/dellemc/openmanage/ome_application_console_preferences_module.html).

- **Only two values are exposed as a discrete choice — there is no
  documented third "iDRAC IP Address" option** in this module's schema.
  A Dell 4.5 User's Guide page (fetched, and this one did render body
  text — see below) independently says the *fallback* when neither
  hostname is known is IP-based display, but that is described as a
  fallback behaviour of the two modes, not a third selectable mode. This
  matches the user's report of exactly two options (iDRAC Hostname /
  System Hostname) rather than contradicting it.
- **Dell's own console/UI label and this repo's user-report label match**:
  the community-facing setting name is "Server Device Naming" and its
  values map `IDRAC_HOSTNAME` ↔ "iDRAC Hostname" and
  `IDRAC_SYSTEM_HOSTNAME` ↔ "System Hostname" — confirmed against a
  synthesized-but-source-cited passage from the **Dell OpenManage
  Enterprise 4.5 User's Guide, "Manage console settings"** page (URL below)
  describing "Server Device Naming... applies to iDRACs only... default
  naming preference for iDRAC devices is the System Hostname. However, if
  an iDRAC lacks a hostname or system hostname, the appliance identifies
  it by its IP address." This page only rendered as a search-engine
  synthesis of its content in this session (see "Sourcing note"), not a
  page fetch this session could requote verbatim, so **treat the exact
  wording as UNVERIFIED and the substance (setting name, two named modes,
  IP-address fallback behaviour) as corroborated, not independently
  confirmed** — settle by an operator opening
  `https://<OME-appliance>/support/manuals/.../manage-console-settings`
  or, better, the appliance's own Application Settings > Console
  Preferences > Discovery Settings page directly.
  ([Dell OpenManage Enterprise 4.5 User's Guide — Manage console
  settings](https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome_4_5_online_help_user_guide/manage-console-settings?guid=guid-5e7831c8-411d-40bf-a4ff-8a34fc57c2bc&lang=en-us))

**Yes, it affects `DeviceName`** — this is the entire premise of the
Ansible module's `DEVICE_PREFERRED_NAME` console setting, and is
independently the exact complaint filed against Dell in the community
thread ["Device Overview, Hostnames instead of iDRAC-Names?"](https://www.dell.com/community/Dell-OpenManage-Enterprise/Device-Overview-Hostnames-instead-of-iDRAC-Names/td-p/7329853),
where an OME 3.2.0 operator reports the console's device overview showing
hostnames rather than iDRAC names (this thread had no reply visible to
this session's fetch, so it is corroborating the *symptom*, not a Dell
explanation of cause — the Ansible module source is what confirms cause).

**Whether it affects `TargetName` specifically**: see section 3 — not
found stated explicitly anywhere in Dell's own docs; inferred with
moderate confidence from how `TargetId`/`TargetName` are populated.

**Yes, it is exposed via REST**, and this is a concrete, citable finding
the task asked for by name. From the same module's source
(`ome_console_pref.py` in the citation above), reading around
`SETTINGS_URL`, `fetch_cp_settings`, and `create_payload`:

- `GET ApplicationService/Settings` returns `{"value": [...]}`, one entry
  per console setting, each shaped
  `{"Name": ..., "DefaultValue": ..., "Value": ..., "DataType": ...,
  "GroupName": ...}`.
- The Server Device Naming (and General Device Naming) policy is **one
  combined setting**, `"Name": "DEVICE_PREFERRED_NAME"`, whose `"Value"`
  is a comma-joined pair like `"PREFER_DNS,PREFER_IDRAC_SYSTEM_HOSTNAME"`
  or `"PREFER_IDRAC_HOSTNAME"` alone — confirmed by the module's own
  `RETURN` documentation block, which is a real, Dell-published example
  response:

  ```json
  {
    "Name": "DEVICE_PREFERRED_NAME",
    "DefaultValue": "SLOT_NAME",
    "Value": "PREFER_DNS,PREFER_IDRAC_SYSTEM_HOSTNAME",
    "DataType": "java.lang.String",
    "GroupName": "DISCOVERY_SETTING"
  }
  ```

  Source: [`ome_application_console_preferences.py`, `RETURN` block, lines
  ~228–240](https://github.com/dell/dellemc-openmanage-ansible-modules/blob/collections/plugins/modules/ome_application_console_preferences.py)
  and `create_payload`'s construction of the same `"PREFER_" + ...` value
  strings at lines ~475–487 of the same file (`GET`/`POST
  ApplicationService/Settings`, `SETTINGS_URL = "ApplicationService/Settings"`,
  line 373).

  So a collector *could* read current policy before trusting
  `TargetName`/`DeviceName`: `GET ApplicationService/Settings`, find the
  entry with `Name == "DEVICE_PREFERRED_NAME"`, and check whether its
  `Value` contains `"PREFER_IDRAC_SYSTEM_HOSTNAME"` (naming is
  hostname-based, distrust `TargetName`/`DeviceName` as an address) versus
  `"PREFER_IDRAC_HOSTNAME"` (iDRAC's own hostname — still not
  guaranteed to be a resolvable/reachable name on the collector's
  network, notably). This is presented as a finding, not a
  recommendation to build it — section 4 below recommends a simpler,
  policy-independent fix instead.

## 2. The field that is always a real IP: `DeviceManagement[N].NetworkAddress`

**Confirmed, from four independent, official Dell scripts, all reading the
exact same field the same way**, none of them ever reading `DeviceName`
when an actual address is needed:

- `PowerShell/Set-PowerState.ps1`:
  ```powershell
  if ($Device.'DeviceManagement'[0].'NetworkAddress' -eq $DeviceIdracIp) {
      $DeviceId = $Device."Id"
  ```
  and, building an operator-facing report row:
  ```powershell
  "Device Name" = $DeviceStatus.DeviceName
  "idrac IP" = $DeviceStatus.DeviceManagement[0]['NetworkAddress']
  ```
  — Dell's own script labels `DeviceManagement[0].NetworkAddress` as the
  **"idrac IP"**, as a field distinct from and alongside `DeviceName`.
  Source: [`PowerShell/Set-PowerState.ps1`, lines 275,
  609](https://github.com/dell/OpenManage-Enterprise/blob/main/PowerShell/Set-PowerState.ps1).
  The `-DeviceIdracIp` parameter it's compared against is typed
  `[System.Net.IPAddress]$DeviceIdracIp` (line 239 of the same file) —
  Dell's own script enforces at the PowerShell type-system level that this
  parameter, and by extension `NetworkAddress`, is an IP address, not a
  hostname string.
- `PowerShell/Invoke-ManageSupportAssistGroups.ps1`, line 367: identical
  pattern.
  ([source](https://github.com/dell/OpenManage-Enterprise/blob/main/PowerShell/Invoke-ManageSupportAssistGroups.ps1))
- `PowerShell/Add-DeviceToStaticGroup.ps1`, line 262: identical pattern.
  ([source](https://github.com/dell/OpenManage-Enterprise/blob/main/PowerShell/Add-DeviceToStaticGroup.ps1))
- `Python/deploy_template.py` and `Python/invoke_refresh_inventory.py`,
  both in `get_device_id`/inline equivalent: resolve a `--device-idrac-ip`
  CLI argument via
  ```python
  device_ids = get_data(authenticated_headers,
      "https://%s/api/DeviceService/Devices" % ome_ip_address,
      "DeviceManagement/any(d:d/NetworkAddress eq '%s')" % device_idrac_ip)
  ...
  if device_id['DeviceManagement'][0]['NetworkAddress'] == device_idrac_ip:
      device_id = device_id['Id']
  ```
  — and, in the same function, resolving a device **by name** is a
  visibly different, separate code path:
  ```python
  if device_name:
      device_id = get_data(authenticated_headers, "...DeviceService/Devices",
          "DeviceName eq '%s'" % device_name)
  ```
  Dell's own script therefore treats `DeviceName` and
  `DeviceManagement[].NetworkAddress` as two different concepts — a name
  you look devices up *by*, and an address you *reach* them at — never the
  same value.
  Source:
  [`Python/deploy_template.py`, `get_device_id`, lines
  180–239](https://github.com/dell/OpenManage-Enterprise/blob/main/Python/deploy_template.py);
  [`Python/invoke_refresh_inventory.py`, lines
  334–351](https://github.com/dell/OpenManage-Enterprise/blob/main/Python/invoke_refresh_inventory.py).

**Shape of the field, as used by every citation above**:
`DeviceManagement` is an **array** on a `/DeviceService/Devices` entry;
every official script indexes `[0]` without further filtering, and one of
Dell's own open GitHub issues on this exact repo
([`dell/OpenManage-Enterprise#87`, "NetworkAddress device filter not
working"](https://github.com/dell/OpenManage-Enterprise/issues/87)) is a
user reporting that the *server-side* OData filter
`DeviceManagement/any(d:d/NetworkAddress eq '...')` does not reliably work
because `NetworkAddress` is a nested property OData does not filter well —
Dell did not fix it (corrected 2026-11-25: the issue is still open, not closed "invalid"; see
`2026-11-25-ome-profile-filter-semantics.md`), i.e. **this is
a known rough edge in querying by this field server-side**,
but it does not cast doubt on the field's meaning or its presence once you
have the full device object (as this repo's collector already fetches
unfiltered — see "Current code" below). The four scripts above all
work around exactly this by fetching devices unfiltered and comparing
`NetworkAddress` client-side, the same shape this repo's own
`OmeClient.get_all("/DeviceService/Devices")` already uses.

**Confirmed via web search of Dell's own hosted docs (not independently
re-verified by a direct fetch this session — see "Sourcing note")**: a
search-engine synthesis citing
[`dell.com/.../lex_techrel_pub/device-service`](https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/lex_techrel_pub/device-service?guid=guid-9c751042-5859-4631-97b0-f99f42557874&lang=en-us)
describes the full `DeviceManagement` object shape as `ManagementId`,
`NetworkAddress`, `MacAddress`, `ManagementType`, `InstrumentationName`,
`DnsName`, `ManagementProfile`, and gives `NetworkAddress` example values
`"10.36.0.30"` and an IPv6 form
`"fe80::f68e:38ff:fecf:15ba"`. **Treat the full field list and the IPv6
example as UNVERIFIED** — this session's direct fetches of that same URL
only ever returned the page's table of contents (see "Sourcing note"),
so this could not be requoted from the page itself. What *is* directly
confirmed, from the scripts above: `NetworkAddress` exists, is indexed at
`[0]` by every official consumer, and is an IP address. Whether a device
can carry a *second* `DeviceManagement` entry (e.g. an IPv6 sibling to an
IPv4 primary) such that `[0]` is not always the right one is exactly what
the IPv6 example (if real) would imply, and is unresolved by this
session — see Open Questions.

## 3. Is `ProfileService/Profiles`'s `TargetName` independently subject to the naming policy?

**Not stated explicitly anywhere Dell publishes that this session could
reach.** What is confirmed, and supports treating it as subject to the
same effect (i.e. **not** immune, contradicting this repo's current
assumption):

- Dell's own official Ansible module for reading profiles,
  `ome_profile_info.py`, documents the full `/ProfileService/Profiles`
  entry shape in its `RETURN` block, including `TargetId`, `TargetName`,
  `TargetTypeId`, alongside `ProfileName`/`ProfileState`. The one
  populated example Dell ships shows an **undeployed** profile:
  `"TargetId": 0, "TargetName": null` — consistent with, and the direct
  primary-source origin of, this repo's own existing claim in
  `docs/dell-collectors.md` that "An undeployed profile has no
  `TargetName`". Source:
  [`plugins/modules/ome_profile_info.py`, `RETURN` block, lines
  120–145](https://github.com/dell/dellemc-openmanage-ansible-modules/blob/collections/plugins/modules/ome_profile_info.py).
  No Dell-published example this session found shows a **deployed**
  profile's populated `TargetName` value, so its literal contents
  (hostname vs. IP string) were not directly observed in any example
  JSON.
- The module's own filtering documentation lists `TargetName` as one of
  the fields profiles can be sorted/filtered by
  (`"...sorted based on ProfileName, TemplateName, TargetTypeId,
  TargetName, ChassisName, ProfileState, ..."` — same file, lines 45–47),
  treating it as an opaque display/filter string, the same way
  `ProfileName` and `ChassisName` are, rather than as a typed network
  address.
- `TargetId` is the integer that actually identifies the target device
  (it is `0` for an undeployed profile, matching `DeviceService/Devices`'
  own `Id`, the same `Id` all four scripts in section 2 resolve to before
  ever touching an address or a name). Dell's own
  **User's Guide** text (this page did render body text this session, see
  full quote below) independently describes what a Profile conceptually
  carries: **"A Profile consists of target-specific attribute values
  along with the BootToISO choices, and iDRAC management IP details of
  the target device."** — Dell's own words describe the Profile's
  target information as "iDRAC management IP details", the same concept
  `DeviceManagement[].NetworkAddress` names on the Device side, not as
  a name. Source: [Dell EMC OpenManage Enterprise 3.8.2 User's Guide,
  "Manage profiles"](https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome_p_382_user_guide/manage-profiles?guid=guid-09499bbe-bc94-42f2-96d6-8fbb6a0814e0&lang=en-us).

**Putting those together**: `TargetName` is very likely populated from
(or kept in sync with) the target `Device`'s own display name once a
profile is deployed — the same `DeviceName` the Server Device Naming
setting is documented to control — rather than being an independently
IP-typed field. This is an **inference**, not a directly quoted Dell
statement that "`TargetName` follows the naming policy". It is, however,
exactly consistent with what was actually observed in production: the
reported failure is `TargetName` containing `ocp4-compute-five-01`, the
OS hostname, for a fleet where the operator suspects System Hostname
naming is active. **Treat "`TargetName` is naming-policy-dependent,
just like `DeviceName`" as the working assumption until an operator can
confirm it directly** by comparing a deployed profile's `TargetName`
against that same device's own `DeviceManagement[0].NetworkAddress` on a
live appliance under both naming-policy settings — see Open Questions.

## 4. Recommendation

Given the above, the field to prefer for the Redfish collection target is
**`DeviceManagement[0].NetworkAddress`** off `/DeviceService/Devices`, not
`TargetName` (Profiles) or `DeviceName` (Devices) — this is what every
piece of Dell's own automation tooling this session found does, without
exception, whenever it actually needs to *reach* a device rather than
just display or look one up by name.

A **join key** worth calling out separately from the *address* question:
this repo's current code
(`OpenManageProvider._discover`, `backend/app/infrastructure/providers/openmanage/provider.py`)
joins a profile to its device by string-matching
`profile.get("TargetName")` against a `device_by_ip` dict keyed on
`str(device.get("DeviceName"))` — i.e. it already relies on `TargetName`
and `DeviceName` being the *same* value to find the right device at all,
before ever reading an address from either. Dell's own scripts never do
this: section 2's citations resolve a device by **`Id`** (an opaque
integer, immune to any naming policy) whenever they have one available,
precisely because name-based joins are the fragile path. `TargetId`
(Profiles) and `Id` (Devices) are that same kind of value on this pair of
endpoints — confirmed present on both resources in sections 2 and 3 above,
though this session found no explicit Dell statement that
`profile.TargetId == device.Id` (as opposed to some other Profile-internal
ID space) — see Open Questions for what would settle that with certainty.

Recommended fallback chain, in order of confidence, to *research* further
before building (this document does not decide the implementation):

1. `DeviceManagement[0].NetworkAddress` off the `/DeviceService/Devices`
   entry joined by device `Id` (via `TargetId`, if confirmed equivalent —
   see Open Questions) — a real IP, confirmed immune to the naming policy
   by every Dell script in section 2.
2. If `DeviceManagement` is empty/absent for a device (unconfirmed
   whether this is possible for any managed iDRAC — no Dell source found
   either affirms or rules this out): today's fallback,
   `TargetName`/`DeviceName`, with the caveat now on record that it may be
   an OS hostname, not an address, whenever Server Device Naming is set to
   System Hostname.
3. Reading `ApplicationService/Settings`'s `DEVICE_PREFERRED_NAME` value
   (section 1) is a way to *detect* which of the above is likely to hold
   fleet-wide, but does not by itself supply a working address — it was
   not found to be worth building only for that.

## Open questions / UNVERIFIED

- **Whether `Profile.TargetId` and `Device.Id` are the same ID space** —
  no Dell source found says so explicitly; it is inferred from both being
  plain integers, from `TargetId: 0` meaning "no device" on an undeployed
  profile (matching no `Id` ever being `0` for a real device in the
  scripts above), and from `deploy_template.py` deploying a template
  by device `Id` via `TargetIds` in a
  `TemplateService.Actions.TemplateService.Deploy` payload immediately
  after resolving one via `get_device_id`. **Settle by**: on a live
  appliance, read one deployed profile's `TargetId` and confirm it equals
  the `Id` of the `/DeviceService/Devices` entry it is believed to name.
- **Whether `TargetName`'s literal value is confirmed to change between
  the two Server Device Naming modes** — this document's section 3
  conclusion is an inference from two indirect facts (User's Guide prose
  calling the Profile's target field "iDRAC management IP details";
  `TargetName` listed as a sortable/filterable string alongside other
  display-name fields), not a directly observed before/after JSON pair.
  **Settle by**: on the user's own appliance, note one deployed profile's
  `TargetName`, flip Server Device Naming
  (`ApplicationService/Settings`'s `DEVICE_PREFERRED_NAME`, or the console
  UI), force a rediscovery, and re-read the same profile's `TargetName`.
  This is the single check that would most directly confirm or refute this
  document's central claim.
- **Whether `DeviceManagement` can be empty for a managed iDRAC** — no
  Dell source found states or denies this. **Settle by**: inspecting
  `DeviceManagement` on a `/DeviceService/Devices` response for every
  device type present in the user's fleet (in particular any device OME
  manages OOB-only vs. also in-band), on a live appliance.
- **Whether `DeviceManagement` can hold more than one entry per device**
  (e.g. an IPv4/IPv6 pair, or in-band plus OOB) such that indexing `[0]`
  is not always correct — a search-engine synthesis of Dell's REST API
  guide (not independently re-fetched and requoted this session) showed
  an IPv6-shaped `NetworkAddress` example, which would be consistent with
  more than one entry existing on some devices, but every official Dell
  script found indexes `[0]` unconditionally with no `ManagementType`
  filter, which this document treats as Dell's own de facto answer
  pending direct confirmation. **Settle by**: reading a raw
  `/DeviceService/Devices` response on the user's own appliance and
  counting `DeviceManagement` array length across the fleet, not just one
  server.
- **The exact console UI location and full wording of "Server Device
  Naming"** — corroborated by a search-engine synthesis of the Dell 4.5
  User's Guide (cited in section 1) but not independently requoted from a
  direct page fetch this session, because this environment's fetcher only
  ever returned that page's navigation shell. **Settle by**: an operator
  screenshotting Application Settings > Console Preferences > Discovery
  Settings on their own appliance, or a future session using a fetcher
  that renders the guide's client-side content.
- **Dell's public Swagger/OpenAPI JSON** (`/api/$metadata` or `/openapi`
  on a live appliance) was never reached this session — no appliance was
  reachable from this environment, and there is no publicly hosted
  equivalent this session could find (unlike UCSPE for Cisco, Dell
  publishes no downloadable OME emulator). **Settle by**: the checklist
  item this document's findings should feed into
  (`docs/field-test-checklist.md`) — pull `$metadata`/`/openapi` from the
  user's own OME appliance and confirm the `DeviceManagement` entity
  type's declared properties directly, which would resolve every
  `UNVERIFIED` item above about field presence/shape in one request.

## Current code, for reference (not changed by this document)

`backend/app/infrastructure/providers/openmanage/mapping.py`,
`identity_from_profile`:

```python
idrac_ip = _opt_str(profile.get("TargetName")) or _opt_str(device.get("DeviceName"))
```

`backend/app/infrastructure/providers/openmanage/provider.py`,
`_discover`, builds the profile-to-device join this value is read from:

```python
device_by_ip = {
    str(device.get("DeviceName")): device
    for device in devices
    if device.get("DeviceName") is not None
}
...
idrac_ip = str(profile.get("TargetName") or "")
identity = identity_from_profile(profile=profile, device=device_by_ip.get(idrac_ip, {}))
```

Both the value used as the Redfish target address (`TargetName`/
`DeviceName`) and the join key connecting a profile to its device
(also `TargetName`/`DeviceName`) are the two fields this document found
the least primary-source support for as network addresses, and the most
support (sections 2 and 4) for being naming-policy-dependent display
strings instead.
