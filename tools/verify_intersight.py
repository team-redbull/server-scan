"""Read-only probe settling the questions `docs/adr/0017` could not.

**Is the API key accepted, what does `TotalMemory` actually mean, and
does anything here name a server in a way this platform can use**? Those
three decide whether the Intersight collector works at all. No
live Intersight call has ever been made against this code — the DevNet
sandbox went offline in August 2026 and Cisco publishes response schemas
without example values, so the mapping is built entirely from the
contract. This tool is how the first real tenant settles it, in minutes,
before a CronJob is ever scheduled.

Writes nothing: no MongoDB connection, no ingest pipeline, no `Manager`
document, no `POST` of any kind. It signs read-only `GET`s, prints a
verdict, and exits.

    uv run python -m tools.verify_intersight

Reads the same `INVENTORY_INTERSIGHT_IP`/`_API_KEY_ID`/`_API_KEY_PEM`
the collector does, so if this works the collector can connect too.

A few small helpers below mirror logic already in `intersight.mapping`
rather than importing its private functions — this is a probe an
operator runs, not a caller entitled to the collector's internals.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.value_objects.site import parse_site_code, site_catalog
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.providers.intersight import mapping
from app.infrastructure.providers.intersight.client import (
    IntersightAuthError,
    IntersightClient,
    IntersightError,
    IntersightForbiddenError,
    IntersightUnreachableError,
)
from app.infrastructure.providers.intersight.signing import IntersightKeyError
from app.infrastructure.providers.ucs_common import normalize_oper_state

# Matches `intersight.mapping._BYTES_PER_MB`: `Size` is "in MB", assumed 2**20.
_MIB = 1024 * 1024


def _int(value: object) -> int | None:
    """
    An integer, or None when the value is absent or not numeric.

    Args:
        value (object): Any reported value.

    Returns:
        int | None: The integer, or None.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the command line.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--show-names",
        type=int,
        default=10,
        metavar="N",
        help="Print the first N resolved server names as a spot check (default 10, 0 to disable).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=200,
        metavar="N",
        help="How many servers to inspect. Kept small by default; this is a probe, not a run.",
    )
    return parser.parse_args(argv)


def _p(text: str = "") -> None:
    """
    Print one line, unbuffered.

    Args:
        text (str): The line.
    """
    print(text, flush=True)


def _header(text: str) -> None:
    """
    Print a section heading.

    Args:
        text (str): The heading.
    """
    _p()
    _p(text)
    _p("-" * len(text))


async def _run(*, show_names: int, sample: int) -> int:
    """
    Probe the configured endpoint and print a verdict.

    Args:
        show_names (int): How many resolved names to print.
        sample (int): How many servers to inspect.

    Returns:
        int: 0 for GOOD, 2 if Intersight isn't configured at all (matching
            `verify_oneview.py`/`verify_ucs_central.py`), 1 for anything
            else an operator must act on.
    """
    settings = get_settings()
    try:
        connection = EnvConnectionResolver(settings).resolve(ManagerType.INTERSIGHT)
    except ManagerNotConfiguredError as exc:
        _p(str(exc))
        return 2

    _header("1. CONNECTION")
    api_key_id, api_key_pem = connection.username, connection.password
    _p(f"endpoint : {connection.endpoint}")
    _p(f"key id   : {api_key_id}")
    _p(f"key      : {'PEM supplied' if '-----BEGIN' in api_key_pem else 'NOT A PEM'}")

    _p("TLS      : certificate verification is DISABLED (unconditional, see IntersightClient)")

    try:
        client = IntersightClient(
            endpoint=connection.endpoint,
            key_id=api_key_id,
            private_key_pem=api_key_pem,
            page_size=min(sample, settings.intersight_page_size),
        )
    except (IntersightKeyError, ValueError) as exc:
        _p(f"\nFAILED before any request was sent: {exc}")
        return 1

    try:
        try:
            await client.health_check()
        except IntersightAuthError as exc:
            _p(f"\nBAD — {exc}")
            return 1
        except IntersightForbiddenError as exc:
            _p(f"\nBAD — {exc}")
            return 1
        except IntersightUnreachableError as exc:
            _p(f"\nBAD — {exc}")
            _p("      An air-gapped site cannot reach intersight.com; it needs an on-prem")
            _p("      Private Virtual Appliance, and INVENTORY_INTERSIGHT_IP set to its FQDN.")
            return 1
        _p("\nauth     : OK — the API key was accepted and inventory is readable.")

        return await _inspect(client, show_names=show_names, sample=sample)
    except IntersightError as exc:
        _p(f"\nFAILED: {exc}")
        return 1
    finally:
        await client.aclose()


async def _inspect(client: IntersightClient, *, show_names: int, sample: int) -> int:
    """
    Read a sample of servers and report what the mapping would make of it.

    Args:
        client (IntersightClient): A connected client.
        show_names (int): How many resolved names to print.
        sample (int): How many servers to inspect.

    Returns:
        int: 0 for GOOD, 1 otherwise.
    """
    settings = get_settings()

    summaries: list[dict[str, Any]] = []
    async for row in client.list_all("compute/PhysicalSummaries"):
        summaries.append(row)
        if len(summaries) >= sample:
            break

    _header("2. WHAT THIS TENANT HOLDS")
    if not summaries:
        _p("no servers at all.")
        _p()
        _p("INCONCLUSIVE — the key works and the API answers, which proves signing, paging")
        _p("               and error handling. It proves nothing about field mapping.")
        _p("               Claim some hardware and run this again.")
        return 1

    modes = Counter(mapping.management_mode(s) for s in summaries)
    _p(f"servers sampled : {len(summaries)}")
    for mode, count in modes.most_common():
        note = ""
        if mode == mapping.MODE_UCSM:
            note = "  <- owned by the UCS Central collector; not collected here by default"
        _p(f"  ManagementMode {mode:<22} {count:>6}{note}")

    # Same parsing as `tools/run_collector.py`, stripped.
    configured_modes = {
        mode.strip() for mode in settings.intersight_management_modes.split(",") if mode.strip()
    }
    collected = [s for s in summaries if mapping.management_mode(s) in configured_modes]
    _p(f"\nservers this collector would ingest: {len(collected)} of {len(summaries)}")

    profiles: dict[str, Any] = {}
    async for row in client.list_all("server/Profiles", select="Moid,Name,AssignedServer"):
        assigned = mapping.moref(row.get("AssignedServer"))
        if assigned:
            profiles[assigned] = row

    _header("3. THE SERVER NAME (the field everything else depends on)")
    _p("The platform parses a server's SITE out of its name and filters the fleet on")
    _p("INVENTORY_COLLECTOR_NAME_PATTERN, so a name sourced wrongly collects nothing.")
    _p()

    named = 0
    sited = 0
    names: list[str] = []
    for summary in collected:
        profile = profiles.get(str(summary.get("Moid") or ""))
        name = mapping.server_name(summary, profile)
        if not name:
            continue
        names.append(name)
        if profile is not None:
            named += 1
        if parse_site_code(name, site_catalog(settings.sites)) is not None:
            sited += 1

    _p(f"servers with an assigned server.Profile : {named} / {len(collected)}")
    _p(f"names carrying a parseable site token   : {sited} / {len(names)}")

    pattern = settings.collector_name_pattern
    matching = [n for n in names if re.search(pattern, n)] if pattern else names
    if pattern:
        _p(f"names matching INVENTORY_COLLECTOR_NAME_PATTERN={pattern!r}: {len(matching)}")
    if show_names and names:
        _p()
        _p(f"first {min(show_names, len(names))} resolved name(s):")
        for name in names[:show_names]:
            _p(f"  {name}")

    await _check_memory_unit(client, collected)
    controller_owner = await _storage_controller_owner_map(client)
    await _check_boot_optimized_storage(client, collected, controller_owner)
    await _check_disk_capacity(client, collected, controller_owner)
    await _check_operstate_vocabulary(client)
    await _check_disk_health(client)

    _header("VERDICT")
    if not matching:
        _p("BAD — no server's name matches INVENTORY_COLLECTOR_NAME_PATTERN, so a real run")
        _p("      would ingest nothing. Either the pattern is wrong for this estate, or the")
        _p("      names are not coming from where this collector looks. Check section 3.")
        return 1
    if named < len(collected):
        _p(f"PARTIAL — {len(collected) - named} server(s) have no assigned profile and fall back")
        _p("          to a label or the summary's own name. That is expected for")
        _p("          standalone-claimed servers and a problem for Intersight-managed ones.")
        _p(f"          {len(matching)} name(s) match the collection pattern, so a run would")
        _p("          collect something. Review the names above before scheduling it.")
        return 1
    _p("GOOD — every collectable server resolved a profile name, and")
    _p(f"       {len(matching)} of them match the collection pattern.")
    _p("       Run `--manager-type INTERSIGHT --dry-run` next, then check the memory note.")
    return 0


async def _check_memory_unit(client: IntersightClient, summaries: list[dict[str, Any]]) -> None:
    """
    Settle `TotalMemory`'s undocumented unit against two documented signals.

    `AvailableMemory` (same object, MB) and a DIMM sum via `memory.Array`
    (MiB) — see docs/cisco-collectors.md, "Units".

    Args:
        client (IntersightClient): A connected client.
        summaries (list[dict[str, Any]]): The sampled servers.
    """
    _header("4. THE TotalMemory UNIT (ADR-0017's highest-risk open item)")
    candidate = next((s for s in summaries if _int(s.get("TotalMemory"))), None)
    if candidate is None:
        _p("no sampled server reported TotalMemory — cannot check.")
        return

    moid = str(candidate.get("Moid"))
    total = _int(candidate.get("TotalMemory")) or 0
    _p(f"server            : {candidate.get('Serial')}")
    _p(f"TotalMemory       : {total}   (no documented unit)")

    available = _int(candidate.get("AvailableMemory"))
    if available:
        _p(f"AvailableMemory   : {available}   (documented 'in MB')")
        if available == total:
            _p("  -> the two agree exactly. Whatever unit AvailableMemory uses,")
            _p("     TotalMemory uses it too. See the DIMM check below for which.")
        else:
            _p(f"  -> they differ (ratio {total / available:.4f}); not the same measure.")

    try:
        arrays = [
            array
            async for array in client.list_all(
                "memory/Arrays",
                select="Moid,ComputeBlade,ComputeRackUnit,CurrentCapacity",
                filter_expr=(f"ComputeBlade.Moid eq '{moid}' or ComputeRackUnit.Moid eq '{moid}'"),
            )
        ]
    except IntersightError as exc:
        _p(f"\ncould not read memory/Arrays: {exc}")
        _p("Filtering on a relationship's Moid may not be supported here. Compare one")
        _p("server's TotalMemory against the sum of its DIMM capacities in the Intersight")
        _p("UI by hand, and record the answer in ADR-0017 — do not skip this.")
        return

    array_moids = {str(a.get("Moid")) for a in arrays if a.get("Moid")}
    if not array_moids:
        _p("\nno memory.Array for this server — cannot reach its DIMMs.")
        return

    dimm_mib = 0
    dimm_count = 0
    try:
        for array_moid in array_moids:
            async for unit in client.list_all(
                "memory/Units",
                select="Moid,Capacity,Presence,MemoryArray",
                filter_expr=f"MemoryArray.Moid eq '{array_moid}'",
            ):
                capacity = _int(unit.get("Capacity"))
                if capacity:
                    dimm_mib += capacity
                    dimm_count += 1
    except IntersightError as exc:
        _p(f"\ncould not read memory/Units: {exc}")
        return

    _p(f"sum of DIMM sizes : {dimm_mib}   (documented MiB, across {dimm_count} DIMM(s))")
    _p()
    if not dimm_mib:
        _p("INCONCLUSIVE — no DIMM capacities came back.")
        return
    if total == dimm_mib:
        _p("SETTLED: TotalMemory is in the same unit as the DIMMs (MiB). The collector's")
        _p("         assumption is correct — record this in ADR-0017 and delete the")
        _p("         caveat, and move the fact into docs/cisco-collectors.md.")
        return
    ratio = total / dimm_mib
    _p(f"MISMATCH — TotalMemory is {ratio:.4f}x the DIMM total.")
    if abs(ratio - 1.048576) < 0.01:
        _p("           That is exactly the MB-vs-MiB ratio: TotalMemory is in decimal MB,")
        _p("           and the collector is over-reporting memory by 4.86%. Change")
        _p("           _BYTES_PER_MB to 1000 * 1000 in")
        _p("           app.infrastructure.providers.intersight.mapping BEFORE scheduling")
        _p("           the CronJob, and update ADR-0017.")
    else:
        _p("           Neither unit explains this. Do not trust memory_total_bytes until")
        _p("           it is understood; report the numbers above in ADR-0017.")


async def _resource_by_owner(
    client: IntersightClient, resource: str, *, select: str, owner_field: str = "StorageController"
) -> dict[str, int] | None:
    """
    Count rows of one resource, keyed by the `Moid` an owner field names.

    A local join, deliberately not the collector's private helpers.

    Args:
        client (IntersightClient): A connected client.
        resource (str): Path under `/api/v1`, e.g.
            `"storage/PhysicalDisks"`.
        select (str): `$select` field list; must include `Moid` and
            `owner_field`.
        owner_field (str): The relationship field naming this row's
            owner, e.g. `"StorageController"` or `"ComputeBoard"`.

    Returns:
        dict[str, int] | None: Row count per owner `Moid`, or None if the
            class does not exist on this Intersight version (some deploys
            predate the M.2 boot-optimized storage subsystem entirely).
    """
    counts: dict[str, int] = {}
    try:
        async for row in client.list_all(resource, select=select):
            owner = mapping.moref(row.get(owner_field))
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
    except IntersightError as exc:
        _p(f"  {resource}: not available — {exc}")
        return None
    return counts


async def _storage_controller_owner_map(client: IntersightClient) -> dict[str, str]:
    """
    Map `storage.Controller` `Moid` -> owning server `Moid`.

    With the `ComputeBoard` fallback, printing how many resolved which
    way — see docs/field-test-checklist.md, Part 1 (e).

    Args:
        client (IntersightClient): A connected client.

    Returns:
        dict[str, str]: `storage.Controller` `Moid` -> owning server
            `Moid`.
    """
    board_owner: dict[str, str] = {}
    async for row in client.list_all("compute/Boards", select="Moid,ComputeBlade,ComputeRackUnit"):
        moid = str(row.get("Moid") or "")
        owner = mapping.moref(row.get("ComputeBlade")) or mapping.moref(row.get("ComputeRackUnit"))
        if moid and owner:
            board_owner[moid] = owner

    direct = 0
    via_board = 0
    unresolved = 0
    controller_owner: dict[str, str] = {}
    async for row in client.list_all(
        "storage/Controllers", select="Moid,ComputeBlade,ComputeRackUnit,ComputeBoard"
    ):
        moid = str(row.get("Moid") or "")
        owner = mapping.moref(row.get("ComputeBlade")) or mapping.moref(row.get("ComputeRackUnit"))
        if owner:
            direct += 1
        else:
            owner = board_owner.get(mapping.moref(row.get("ComputeBoard")) or "")
            if owner:
                via_board += 1
            else:
                unresolved += 1
        if moid and owner:
            controller_owner[moid] = owner

    _p(f"storage.Controller rows: {direct} joined via ComputeBlade/ComputeRackUnit directly")
    if via_board:
        _p(
            f"                         {via_board} joined ONLY via ComputeBoard — expected on"
            " this hardware generation. IntersightProvider._owning_server's board_owner"
            " fallback (added 2026-09-01 for this exact finding) already covers it; this"
            " count is informational, not a warning."
        )
    if unresolved:
        _p(f"                         {unresolved} joined via neither — genuinely unowned")
    return controller_owner


async def _check_boot_optimized_storage(
    client: IntersightClient, summaries: list[dict[str, Any]], controller_owner: dict[str, str]
) -> None:
    """
    Check whether boot-optimized M.2/SD storage explains a zero-drive report.

    `FlexUtil`/`FlexFlash` classes, joined through `compute.Board` —
    ADR-0017's UNVERIFIED list, items 11 and 12.

    Args:
        client (IntersightClient): A connected client.
        summaries (list[dict[str, Any]]): The sampled servers.
        controller_owner (dict[str, str]): `storage.Controller` `Moid` ->
            owning server `Moid`, from `_storage_controller_owner_map`.
    """
    _header("5. BOOT-OPTIMIZED STORAGE — does M.2/SD explain a 0-drive report?")

    board_owner: dict[str, str] = {}
    async for row in client.list_all("compute/Boards", select="Moid,ComputeBlade,ComputeRackUnit"):
        moid = str(row.get("Moid") or "")
        owner = mapping.moref(row.get("ComputeBlade")) or mapping.moref(row.get("ComputeRackUnit"))
        if moid and owner:
            board_owner[moid] = owner

    disks_by_controller = await _resource_by_owner(
        client, "storage/PhysicalDisks", select="Moid,StorageController"
    )
    disks_by_server: dict[str, int] = {}
    for controller_moid, count in (disks_by_controller or {}).items():
        owner = controller_owner.get(controller_moid)
        if owner:
            disks_by_server[owner] = disks_by_server.get(owner, 0) + count

    async def _drives_by_server(
        controller_resource: str, drive_resource: str, owner_field: str
    ) -> dict[str, int]:
        """
        Count boot-optimized drives per server, for one generation (FlexUtil or FlexFlash).

        Args:
            controller_resource (str): The controller class's path.
            drive_resource (str): The drive class's path.
            owner_field (str): The drive's relationship field naming its
                controller.

        Returns:
            dict[str, int]: Drive count per server `Moid`.
        """
        controller_owner: dict[str, str] = {}
        try:
            async for row in client.list_all(controller_resource, select="Moid,ComputeBoard"):
                moid = str(row.get("Moid") or "")
                owner = board_owner.get(mapping.moref(row.get("ComputeBoard")) or "")
                if moid and owner:
                    controller_owner[moid] = owner
        except IntersightError as exc:
            _p(f"  {controller_resource}: not available — {exc}")
            return {}
        drives_by_controller = (
            await _resource_by_owner(
                client, drive_resource, select=f"Moid,{owner_field}", owner_field=owner_field
            )
            or {}
        )
        by_server: dict[str, int] = {}
        for controller_moid, count in drives_by_controller.items():
            owner = controller_owner.get(controller_moid)
            if owner:
                by_server[owner] = by_server.get(owner, 0) + count
        return by_server

    flexutil = await _drives_by_server(
        "storage/FlexUtilControllers", "storage/FlexUtilPhysicalDrives", "StorageFlexUtilController"
    )
    flexflash = await _drives_by_server(
        "storage/FlexFlashControllers",
        "storage/FlexFlashPhysicalDrives",
        "StorageFlexFlashController",
    )

    if disks_by_controller is None:
        _p("\ncould not read storage/PhysicalDisks — cannot correlate. See message above.")
        return

    _p(f"\n{'server':<20}{'PhysicalDisks':>15}{'FlexUtil':>12}{'FlexFlash':>12}")
    explains_something = False
    for summary in summaries:
        moid = str(summary.get("Moid") or "")
        serial = str(summary.get("Serial") or moid)
        disks = disks_by_server.get(moid, 0)
        util = flexutil.get(moid, 0)
        flash = flexflash.get(moid, 0)
        note = ""
        if disks == 0 and (util > 0 or flash > 0):
            note = "  <- boot-optimized storage explains the 0-drive report"
            explains_something = True
        _p(f"{serial:<20}{disks:>15}{util:>12}{flash:>12}{note}")

    _p()
    if explains_something:
        _p("SETTLED: at least one server with zero storage.PhysicalDisk rows has boot-")
        _p("         optimized drives instead. Worth adding storage.FlexUtilController/")
        _p("         FlexUtilPhysicalDrive (and FlexFlash if any rows appeared there) to")
        _p("         the collector — see ADR-0017's UNVERIFIED list, item 11.")
    elif any(disks_by_server.get(str(s.get("Moid") or ""), 0) == 0 for s in summaries):
        _p("INCONCLUSIVE: at least one server still reports zero drives across every")
        _p("              storage class this probe checked. Either it genuinely has none,")
        _p("              or there is a storage class not covered here.")
    else:
        _p("N/A: every sampled server reported at least one storage.PhysicalDisk row.")


def _drive_capacity_bytes(disk: Mapping[str, Any]) -> int | None:
    """
    A drive's capacity in bytes, mirroring `intersight.mapping._capacity_bytes` exactly.

    Args:
        disk (Mapping[str, Any]): A `storage.PhysicalDisk` row.

    Returns:
        int | None: The capacity, or None when neither field parses.
    """
    exact = _int(disk.get("NonCoercedSizeBytes"))
    if exact:
        return exact
    size_mb = _int(disk.get("Size"))
    return size_mb * _MIB if size_mb else None


async def _check_disk_capacity(
    client: IntersightClient, summaries: list[dict[str, Any]], controller_owner: dict[str, str]
) -> None:
    """
    Flag every drive whose capacity the mapping cannot parse.

    Prints the raw `NonCoercedSizeBytes`/`Size` it sent — see
    docs/field-test-checklist.md, Part 1 (e).

    Args:
        client (IntersightClient): A connected client.
        summaries (list[dict[str, Any]]): The sampled servers, for
            resolving a flagged drive's serial in the printout.
        controller_owner (dict[str, str]): `storage.Controller` `Moid` ->
            owning server `Moid`, from `_storage_controller_owner_map`.
    """
    _header("6. DISK CAPACITY — flags any drive the mapping could not size")

    serial_by_server = {
        str(s.get("Moid") or ""): str(s.get("Serial") or s.get("Moid") or "?") for s in summaries
    }
    select = "Moid,DiskId,Model,Type,Protocol,Size,NonCoercedSizeBytes,StorageController"
    total = 0
    flagged = 0
    try:
        async for disk in client.list_all("storage/PhysicalDisks", select=select):
            total += 1
            if _drive_capacity_bytes(disk) is not None:
                continue
            flagged += 1
            controller = mapping.moref(disk.get("StorageController"))
            server = controller_owner.get(controller or "") if controller else None
            serial = serial_by_server.get(server or "", server or "unknown server")
            _p(
                f"  {serial}  disk {disk.get('DiskId') or disk.get('Moid')}"
                f"  {disk.get('Model') or '—'}"
            )
            _p(f"    Type={disk.get('Type')!r}  Protocol={disk.get('Protocol')!r}")
            _p(
                f"    Size={disk.get('Size')!r}"
                f"  NonCoercedSizeBytes={disk.get('NonCoercedSizeBytes')!r}"
            )
    except IntersightError as exc:
        _p(f"could not read storage/PhysicalDisks: {exc}")
        return

    _p(f"\n{flagged} of {total} sampled drive(s) could not be sized.")
    if flagged:
        _p("Compare Type/Protocol above against the drives that DID size correctly —")
        _p("a difference there (e.g. an NVMe drive alongside sized SAS/SATA ones) points")
        _p("at a protocol-specific field this collector does not read yet. Report the raw")
        _p("values above; ADR-0017's storage section is where the fix would land.")


def _mapped_drive_health(
    health: str | None, drive_state: str | None, failure_predicted: str
) -> str:
    """
    A local mirror of `mapping._drive_health`'s logic, for reporting only.

    Args:
        health (str | None): The raw `Health` field.
        drive_state (str | None): The raw `DriveState` field.
        failure_predicted (str): The raw `FailurePredicted` field,
            stringified (Intersight returns a real bool, but this probe
            only ever compares it as text, same as the mapping does).

    Returns:
        str: HEALTHY, WARNING, CRITICAL or UNKNOWN.
    """
    raw = (health or drive_state or "").lower()
    if raw in {"good", "healthy", "online", "optimal", "jbod", "unconfigured good"}:
        return "HEALTHY"
    if raw in {"warning", "degraded", "predictive-failure", "predicted-failure", "rebuilding"}:
        return "WARNING"
    if raw in {"critical", "bad", "failed", "offline", "unconfigured bad", "foreign"}:
        return "CRITICAL"
    if failure_predicted.lower() == "true":
        return "WARNING"
    return "UNKNOWN"


async def _check_disk_health(client: IntersightClient) -> None:
    """
    Cross-check every raw Health/DriveState/FailurePredicted combination this tenant reports.

    Same shape of check as section 7's OperState vocabulary, one field
    over. See ADR-0017's "second field pass" section.

    Args:
        client (IntersightClient): A connected client.
    """
    _header("8. DISK HEALTH VOCABULARY — why so many drives read health=UNKNOWN")

    select = "Moid,Health,DriveState,FailurePredicted"
    counts: Counter[tuple[str, str, str]] = Counter()
    total = 0
    try:
        async for disk in client.list_all("storage/PhysicalDisks", select=select):
            total += 1
            key = (
                str(disk.get("Health")),
                str(disk.get("DriveState")),
                str(disk.get("FailurePredicted")),
            )
            counts[key] += 1
    except IntersightError as exc:
        _p(f"could not read storage/PhysicalDisks: {exc}")
        return

    if total == 0:
        _p("no drives sampled — see section 5/6 above.")
        return

    unknown_total = 0
    _p(f"{'Health':<14}{'DriveState':<20}{'FailurePredicted':<18}{'count':>7}  mapped")
    for (health, state, predicted), n in counts.most_common():
        mapped = _mapped_drive_health(
            None if health == "None" else health,
            None if state == "None" else state,
            predicted,
        )
        flag = "  <- not recognized" if mapped == "UNKNOWN" else ""
        _p(f"{health!r:<14}{state!r:<20}{predicted!r:<18}{n:>7}  -> {mapped}{flag}")
        if mapped == "UNKNOWN":
            unknown_total += n

    _p(f"\n{unknown_total} of {total} sampled drive(s) read health=UNKNOWN.")
    if unknown_total:
        _p("If `Health`/`DriveState` above show a real value rather than `None`, it is a")
        _p("spelling `_drive_health` does not recognize — add it to the recognized sets in")
        _p("`intersight/mapping.py`. If both are `None`, this tenant's controller genuinely")
        _p("does not report either field for that drive, which is a capability gap, not a")
        _p("mapping bug — the same distinction section 7 draws for an empty OperState.")


async def _operstate_values(client: IntersightClient, resource: str) -> Counter[str]:
    """
    Every distinct raw `OperState` string one MO class reports, with counts.

    Args:
        client (IntersightClient): A connected client.
        resource (str): Path under `/api/v1`, e.g. `"equipment/Psus"`.

    Returns:
        Counter[str]: Raw `OperState` value -> how many rows reported it.
            Empty if the class does not exist on this Intersight version.
    """
    counts: Counter[str] = Counter()
    try:
        async for row in client.list_all(resource, select="Moid,OperState"):
            counts[str(row.get("OperState"))] += 1
    except IntersightError as exc:
        _p(f"  {resource}: not available — {exc}")
    return counts


async def _check_operstate_vocabulary(client: IntersightClient) -> None:
    """
    Cross-check every raw `OperState` value this tenant reports against `normalize_oper_state`.

    See ADR-0017's "second field pass" section for why this exists.

    Args:
        client (IntersightClient): A connected client.
    """
    _header("7. OperState VOCABULARY — does normalize_oper_state recognize what this tenant sends?")

    classes = {
        "equipment/Psus": "PSU health",
        "graphics/Cards": "GPU health",
        "adapter/HostEthInterfaces": "vNIC oper_state",
        "adapter/ExtEthInterfaces": "physical port oper_state",
    }
    any_unrecognized = False
    for resource, used_for in classes.items():
        counts = await _operstate_values(client, resource)
        if not counts:
            continue
        _p(f"\n{resource}  ({used_for}):")
        for raw, n in counts.most_common():
            mapped = normalize_oper_state(raw)
            flag = "  <- not recognized" if mapped == "UNKNOWN" else ""
            _p(f"  {raw!r:<20} x{n:<6} -> {mapped}{flag}")
            if flag:
                any_unrecognized = True

    _p()
    if any_unrecognized:
        _p("ACTION: at least one raw OperState value above maps to UNKNOWN. Add it to")
        _p("        ucs_common._OPER_STATE_MAP with the UP/DOWN/DISABLED it actually means —")
        _p("        check the same server in the Intersight UI to know which. A value with")
        _p("        no failed hardware to compare against should still be added as UP if the")
        _p("        UI shows the server healthy; DOWN/DISABLED values may need a fault to")
        _p("        surface at all, so their absence here is not proof they're covered.")
    else:
        _p("Every raw OperState value observed maps to something other than UNKNOWN.")
        _p("This does not prove DOWN/DISABLED are correctly recognized too, only that")
        _p("nothing seen on this tenant was silently dropped to UNKNOWN.")


def main(argv: list[str] | None = None) -> None:
    """
    Entry point.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.
    """
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(show_names=args.show_names, sample=args.sample)))


if __name__ == "__main__":
    main()
