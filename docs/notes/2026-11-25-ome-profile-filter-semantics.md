# OME `$filter ... eq` semantics on ProfileService/Profiles and DeviceService/Devices (2026-11-25)

Summary: Dell never documents what `eq` does on string properties, and Dell's own GitHub tracker holds a
report (issue #87) that `eq` on a Devices IP-like string matched a longer value (`192.168.1.4` returned
`192.168.1.45`), which is the same prefix symptom the operator sees; Dell did not answer it. Dell's
Ansible modules therefore never trust the first row of an `eq` result for profiles: they re-check
`ProfileName` for equality client-side. Nothing documents `?TargetName=...` as a query form, a default sort,
or a fixed page size. The Dell guide PDFs and manual pages returned HTTP 403 to every fetcher here, so
"Data filtering/sorting/pagination" prose was NOT read directly. Label key: CONFIRMED (read at the cited
source) / NOT FOUND (searched, absent) / INFERRED (my reasoning).

SHAs read: ansible modules `16345e55c5596037f39b62d4ae6a4cb9e7c65a83` (branch `collections`),
`dell/OpenManage-Enterprise` `59e87574ec4a6c5f2026af79d21f5077bb0e4e13` (main).

## 1. How `eq` is evaluated

- CONFIRMED (anecdotal, one reporter): `eq` on a nested string matched a longer value. Issue
  https://github.com/dell/OpenManage-Enterprise/issues/87 (comment by grantcurell, 2020-11-25):
  "`DeviceManagement/any(d:d/NetworkAddress eq '%s')` where device_idrac_ip is 192.168.1.4 matches entries
  with idrac IP 192.168.1.45 ... with `eq` it seems like that shouldn't happen." Issue still open, 3
  comments, no Dell reply to that comment (API `.../issues/87/comments`). OME version not stated.
  (The earlier note `2026-09-openmanage-device-naming-and-ip-address.md` says Dell closed #87 "invalid";
  the tracker shows it OPEN, and Dell's only reply was to the earlier `contains(...)` question. That older
  line is wrong.)
- CONFIRMED (Dell staff answer in the same issue, 2020-11-16, RaajeevKalyan): the suggested form for the
  nested address is `DeviceManagement/any(d:contains(d/NetworkAddress,'<ip>'))` - i.e. Dell itself
  recommends a substring function, which also over-matches by construction.
- NOT FOUND: any Dell statement that `eq` is "exact", "case-insensitive", "startswith" or "LIKE" for
  `TargetName`, `ProfileName`, `DeviceName`, `DeviceServiceTag`. The only direct report of prefix
  behaviour is the NetworkAddress one above.
- INFERRED: OME's `eq` on string properties is a prefix/LIKE match on at least some properties/versions, and
  `TargetName` on Profiles behaves the same way (it is the operator's observation; no Dell source for it).
  Treat as unverified until a live probe (see Open questions).
- CONFIRMED: `contains(Prop,'x')` works on Devices (community post, 2020-07-27, answer by the accepted
  reply: "I tried contains function and it worked but endswith and startswith did not"; the failing
  request `/api/DeviceService/Devices?$filter=startswith(DeviceName,'iDRAC')` returned 501 Not
  Implemented). https://www.dell.com/community/en/conversations/dell-openmanage-enterprise/api-string-function-such-as-contains-endswith-and-startswith/647f8abdf4ccf8a8deab32d4
  So `startswith`/`endswith` are not usable; `substringof` NOT FOUND in any Dell source.
- CONFIRMED: Dell's own scripts use `contains(...)` on other services (`ReportService/ReportDefs`,
  `PowerService/EPR`, `AlertService/Alerts`): `Python/get_power_manager_reports.py:96`,
  `Python/get_power_manager_epr.py:104-106` (exact `eq` only for `Enabled`/`Type`, `contains` for strings).
  https://github.com/dell/OpenManage-Enterprise/tree/main/Python
- CONFIRMED: Dell's guide index (v3.2 HTML manual) lists examples `$filter=StatusType eq 2000` and
  `$filter=contains(UserName,'admin')` only; body text unreadable (403/truncated).
  https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome-3.2_omem-1.00.10_apiguide/get-method-for-validationerrors

## 2. `?TargetName=...` and "Console" paths

- NOT FOUND: any documented plain query parameter `?TargetName=<v>` (or any property as a bare
  parameter) on `/api/ProfileService/Profiles`. Every Dell source found uses `$filter=` (list in section 4).
  A bare `?TargetName=` is, INFERRED, either ignored (returns the whole collection, in default order) or
  a vendor quirk; if the operator's result was "prefix matches" with a bare parameter it was likely a
  server-side search parameter we cannot document. Settle with a live probe.
- NOT FOUND: a `Console` path under `/api/` related to profiles in any source read. Documented path:
  `/api/ProfileService/Profiles` (Ansible `PROFILE_VIEW`, `ome_profile_info.py`).
- CONFIRMED: Ansible `ome_profile_info` lists the fields profiles "can be fetched and sorted based on":
  `ProfileName, TemplateName, TargetTypeId, TargetName, ChassisName, ProfileState, LastRunStatus,
  ProfileModified` (`plugins/modules/ome_profile_info.py:45-48`, passed straight through as `$<key>`
  at lines ~355-358).

## 3. Sort order, `$orderby`, paging

- NOT FOUND: any documented default sort for Profiles or Devices. Treat order as unspecified.
- `$orderby`: documented in the guide per a search-result summary of the v3.0 guide
  (`<Collection API>?$orderby=<attribute> <asc/desc>`); the PDF itself was 403, so INFERRED-from-summary.
  Dell sample use CONFIRMED: `Python/get_alerts.py:28-30,361-365` (`orderby=TimeStamp asc`), and an
  example in the v3.2 manual nav: `GroupService/Groups(500)/AllLeafDeviceSummaries?$filter=Type eq 4000&$top=25&$skip=0&$orderby=Identifier asc`
  (search-result excerpt). No source states `$orderby` works on Profiles specifically.
- CONFIRMED: `$top`/`$skip` are the paging parameters (Dell staff, community
  https://www.dell.com/community/Dell-OpenManage-Enterprise/OME-API-Rest-query-many-devices/m-p/5907209:
  "$top = the number of records to get per page (just set this really high...)").
- CONFIRMED (observed behaviour, not a doc): the default page is 50 rows. Ansible issue #499 (OME 3.10.1,
  233 devices, only 50 returned) https://github.com/dell/dellemc-openmanage-ansible-modules/issues/499
  and `get_alerts.py:197-205` ("typically more than 50 devices ... results are still delivered in pages").
  NOT FOUND: a documented maximum `$top`; Dell scripts use `$top=10000000` (`get_power_manager_alerts.py:89`).
- CONFIRMED: `@odata.count` (total matching) and `@odata.nextLink` are returned; Dell's helper follows
  nextLink until absent: `plugins/module_utils/ome.py:304-322` (`get_all_items_with_pagination`).
  A paged-with-filter defect (#502, OME 3.10, "not appending nextLink" under a filter) was closed as a
  duplicate of #499 and fixed in Ansible collection 8.1.0 (PR #512) - a client-side bug, not an OME one.
- CONFIRMED: Dell's PowerShell module appends the filter again to each nextLink
  (`Get-OMEProfile.ps1`: `$NextLinkUrl = $BaseUri + ....'@odata.nextLink' + "&" + $Filter`)
  https://www.powershellgallery.com/packages/DellOpenManage/3.8.0/Content/Public%5COME%5CGet-OMEProfile.ps1
  (INFERRED reason: the nextLink may not carry the filter). Our client uses nextLink only; see section 5.
- CONFIRMED: Ansible encodes spaces as `%20`, because "Ome filtering does not work as expected when '+' is
  passed" (`module_utils/ome.py:154-156`).

## 4. What Dell's own code does for an exact match

All URLs under https://github.com/dell/dellemc-openmanage-ansible-modules/blob/collections/ unless noted.

- Profiles by name, then CLIENT-SIDE equality check - the strongest evidence the filter is not exact:
  - `plugins/modules/ome_profile_info.py:351`: `query[QUERY_FILTER_PARAM] = f"ProfileName eq '{value}'"`
  - same file `:364-370`: `for xp in prof_list: if xp.get("ProfileName") == pname: xprofs.append(xp); break`
  - `plugins/modules/ome_profile.py:451-457` `match_profile`: `if (xtype.get(search_key) == identifier_value) or xtype.get('DeploymentTaskId'): return xtype`
    (called at ~`:482-485` after `ProfileName eq '{0}'`). Note the `or DeploymentTaskId` clause also accepts
    any row with a deployment task, so it is not a clean exact check.
- Devices by service tag with equality re-check: `plugins/modules/ome_device_info.py:232-236`:
  `query = "DeviceServiceTag eq '{0}'"` ... `if value and value[0]["DeviceServiceTag"] == tag:`.
  `module_utils/ome.py:295-301` (`get_device_id_from_service_tag`) takes `value[0]` WITHOUT a re-check.
- Assignment check compares `prof.get('TargetName') == target_id` client-side (`ome_profile.py:702`).
- Sample `Python/get_device_inventory.py:59-76` (OpenManage-Enterprise repo): `$filter=DeviceName eq '<v>'`,
  then prints "WARNING: Filter returned more than one result. Ignore all results except the first." and
  takes `value[0]` - a name can legitimately repeat; it does not prove prefix matching.
- IP lookups: Dell staff answer is `contains(...)` (issue #87); the older note records that Dell scripts fetch
  Devices unfiltered and compare `NetworkAddress` client-side.
- NOT FOUND: any Dell code that uses `startswith`/`substringof`, or `$orderby` to make a filter unique.
- Safest documented-by-practice recipe (INFERRED): filter by `Id` (`Profiles(<id>)` is used by Ansible
  `ome_profile_info.py` `profile_id` -> `PROFILE_VIEW + "(<id>)"`, line ~347) or by `DeviceServiceTag`
  (works, issue #87 text), then compare the returned property to the wanted value in client code, and walk
  every page.

## 5. Affected versions

- NOT FOUND: any Dell statement of affected/fixed OME versions for loose `eq`. Reports exist against
  OME 3.10.x (Ansible #499/#502; #87 gives no version, filed 2020-11). Ansible fix 8.1.0 concerns paging only.

## 6. Implication for `provider.py:333-340` (written before the fix; resolved 2026-11-25, see ADR-0069)

`profiles[0]` after `TargetName eq '<display_name>'` / `ProfileName eq '<name>'` (lines 330-340) is unsafe
under all of the above; per sections 1 and 4 a client-side equality check across ALL pages (a
`next_link`-following `get_all` already exists, `client.py:~205-225`) is what Dell's own modules do.

## Open questions / UNVERIFIED

**Resolved 2026-11-25:** the prefix behaviour of `eq` (question 1) was measured live on the operator's OME; see `docs/dell-collectors.md` and ADR-0069. The rest below is as written before that probe.

1. Whether `eq` on `TargetName`/`ProfileName`/`DeviceName` is prefix, contains or exact on the operator's
   OME build: settle with live GETs of `Profiles?$filter=TargetName eq '10.0.0.1'` and a name known to be a
   prefix of another, with `@odata.count`, plus a case-flipped value and a trailing-space value.
2. What bare `?TargetName=10.0.0.1` does (ignored vs. filtering): one live GET, compare `@odata.count` with
   the unfiltered count.
3. Default order and whether `$orderby=TargetName` is accepted on Profiles: one live GET.
4. Dell's official "Data filtering / sorting / pagination" text and any maximum page size: needs a browser
   download of `dell-openmanage-enterprise_api-guide*_en-us.pdf` (all fetchers here got 403).
5. Whether `@odata.nextLink` under a filter carries the filter (the PowerShell module re-appends it): inspect a
   live nextLink for a filtered query with more than 50 hits.
