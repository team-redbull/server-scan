"""Read-only probe answering questions `docs/adr/0022` could not settle without live HPE OneView.

No OneView call has ever been made against this code. There is no HPE
equivalent of Cisco's UCS Platform Emulator — the 60-day OneView trial is
a real appliance, not a hardware simulator, so with no HPE hardware
attached it enumerates nothing — which puts this collector in the same
unvalidated state ADR-0017 records for Intersight. Every field mapping
here was built from HPE's own API Reference and is unproven.

This tool settles, in one run against a real appliance:

1. Does paging get past `/rest/server-profiles`' documented 256 ceiling,
   or is the list truncated forever?
2. Do the top-level hardware fields (`memoryMb`, `processorCount`,
   `processorCoreCount`, `portMap`) populate on an **iLO 4** server, or
   does an iLO-4 server give us identity and nothing else?
3. What does `mpModel` actually contain, per generation?
4. Which `mpIpAddresses` entry is the reachable one, and is there always
   one?
5. What does the appliance do when `X-Api-Version` is omitted?
6. Does `serverName` contain anything on a host with no HPE AMS?
7. Do GPUs also appear under `/processors` as `ProcessorType: "GPU"`?
8. Do the GPU model strings OneView reports match this platform's GPU
   catalog, or does every HPE card report unknown VRAM?
9. **Does `processorCount * processorCoreCount` equal the real core
   count?** `/processors` reports each socket's `TotalCores`, so summing
   it is a direct check on the whole-system core mapping. If it
   disagrees, every server's core count is wrong fleet-wide.
10. Does `expand=all` return `PowerSupplies`, or does each server cost a
    `/powerSupplies` call? That is the difference between a ~15-request
    sweep and a ~2500-request one.

Writes nothing: no MongoDB connection, no ingest pipeline, no `Manager`
document, no POST other than the login it needs, and it logs out.

    uv run python -m tools.verify_oneview

Reads the same `INVENTORY_ONEVIEW_IP`/`_USERNAME`/`_PASSWORD` the
collector does, so if this works the collector can connect too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any

from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.site import parse_site_code, site_catalog
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.providers.oneview.client import (
    DEFAULT_PAGE_SIZE,
    MAX_TESTED_API_VERSION,
    OneViewClient,
    OneViewConnectionError,
)
from app.infrastructure.providers.oneview.mapping import (
    DEVICES,
    LOCAL_STORAGE,
    LOCAL_STORAGE_V2,
    POWER_SUPPLIES,
    ilo_generation,
    management_processor_address,
    profile_from,
    psus_from,
    server_from,
    subresource,
    subresource_data,
)

_UNSET = "—"


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
        "--sample",
        type=int,
        default=25,
        metavar="N",
        help="How many servers to detail. This is a probe, not a run (default 25).",
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


def _present(value: object) -> str:
    """
    Render whether a field came back populated.

    "OneView reported this" versus "reported nothing" is the point of this tool.

    Args:
        value (object): The raw reported value.

    Returns:
        str: The value, or a dash when it is absent, empty or zero.
    """
    if value is None or value == "" or value == 0 or value == [] or value == {}:
        return _UNSET
    return str(value)


async def _check_core_count(client: OneViewClient, hardware: list[dict[str, Any]]) -> str:
    """
    Cross-check the whole-system core count against `/processors`.

    `processorCount * processorCoreCount` versus the sum of each socket's
    `TotalCores` — open question 0 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        client (OneViewClient): A logged-in client.
        hardware (list[dict[str, Any]]): Server-hardware members.

    Returns:
        str: A one-line verdict, repeated in the run's closing summary.
    """
    _p()
    _p("=" * 72)
    _p("HEADLINE — does processorCount * processorCoreCount equal the real core count?")
    _p("=" * 72)

    checked = mismatched = 0
    for member in hardware:
        sockets = member.get("processorCount")
        per_socket = member.get("processorCoreCount")
        uri = str(member.get("uri") or "")
        if not isinstance(sockets, int) or not isinstance(per_socket, int) or not uri:
            continue
        response = await client.raw_get(f"{uri}/processors")
        if response.status_code != 200:
            _p(f"  {member.get('name')}: /processors -> {response.status_code}, cannot check")
            continue
        body = response.json() if response.content else {}
        data = body.get("data")
        rows = data.get("Members") if isinstance(data, dict) else data
        sockets_reported = [row for row in (rows or []) if isinstance(row, dict)]
        totals = [
            row["TotalCores"] for row in sockets_reported if isinstance(row.get("TotalCores"), int)
        ]
        if not totals:
            _p(
                f"  {member.get('name')}: collectionState="
                f"{body.get('collectionState')}, no TotalCores reported"
            )
            continue
        checked += 1
        expected = sockets * per_socket
        actual = sum(totals)
        threads = [row.get("TotalThreads") for row in sockets_reported]
        verdict = "OK" if expected == actual else "MISMATCH"
        if expected != actual:
            mismatched += 1
        _p(
            f"  {verdict:<8} {member.get('name')}: "
            f"{sockets} x {per_socket} = {expected} vs sum(TotalCores) = {actual}"
            f"   (TotalThreads per socket: {threads})"
        )
        if checked >= 5:
            break

    if not checked:
        verdict = (
            "CORE COUNT: INCONCLUSIVE — /processors returned no TotalCores on any sampled server."
        )
    elif mismatched:
        verdict = (
            f"CORE COUNT: WRONG on {mismatched}/{checked} servers — the cpu_cores mapping "
            "under- or over-reports the whole fleet. Fix before scheduling anything."
        )
    else:
        verdict = f"CORE COUNT: CONFIRMED on {checked}/{checked} sampled servers."
    _p()
    _p(f"  >>> {verdict}")
    return verdict


async def _report_power_supplies(client: OneViewClient, hardware: list[dict[str, Any]]) -> None:
    """
    Report which route supplies PSUs, and what one server's look like.

    `expand=all` for free, or `/powerSupplies` at one call per server —
    see docs/hpe-collectors.md, "Power supplies".

    Args:
        client (OneViewClient): A logged-in client.
        hardware (list[dict[str, Any]]): Server-hardware members.
    """
    _header("10. Power supplies (open question: does expand=all include them?)")
    from_expand = sum(1 for m in hardware if subresource_data(m, POWER_SUPPLIES) is not None)
    _p(f"  servers whose expand=all carried {POWER_SUPPLIES}: {from_expand}/{len(hardware)}")
    if from_expand:
        _p("  -> PSUs are FREE. The per-server call is unnecessary; consider defaulting")
        _p("     INVENTORY_ONEVIEW_COLLECT_PSUS off and reading them from the sweep.")
    else:
        _p(f"  -> PSUs cost one /powerSupplies call per server ({len(hardware)} here).")
        _p("     That is what INVENTORY_ONEVIEW_COLLECT_PSUS gates.")

    for member in hardware:
        uri = str(member.get("uri") or "")
        if not uri:
            continue
        response = await client.raw_get(f"{uri}/powerSupplies")
        _p()
        _p(f"  GET {uri}/powerSupplies -> {response.status_code}")
        if response.status_code != 200:
            return
        body = response.json() if response.content else {}
        data = body.get("data")
        rows = data.get("Members") if isinstance(data, dict) else data
        _p(f"      collectionState : {body.get('collectionState')}")
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        for raw, mapped in zip(rows, psus_from(rows) or (), strict=False):
            oem = raw.get("Oem", {})
            hpe = oem.get("Hpe", {}) if isinstance(oem, dict) else {}
            status = hpe.get("PowerSupplyStatus", {}) if isinstance(hpe, dict) else {}
            _p(
                f"      psu {mapped['id']}: {mapped['model'] or _UNSET}  "
                f"{mapped['capacity_watts'] or '?'}W  health={mapped['health']}  "
                f"(Status.Health={((raw.get('Status') or {}).get('Health'))}, "
                f"Oem.Hpe.PowerSupplyStatus.State={status.get('State')})"
            )
        return


async def _probe_version(client: OneViewClient) -> None:
    """
    Report what the appliance supports and what this run will send.

    Also fetches the same collection with and without `X-Api-Version` —
    open question 5 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        client (OneViewClient): A logged-in client.
    """
    _header("1. API version")
    _p(f"  collector will send X-Api-Version: {client.api_version}")
    _p(f"  newest version this mapping was written against: {MAX_TESTED_API_VERSION}")

    path = "/rest/server-hardware?start=0&count=1"
    with_header = await client.raw_get(path)
    without_header = await client.raw_get(path, send_version=False)
    _p()
    _p("  OPEN QUESTION 5 — what happens when X-Api-Version is omitted:")
    for label, response in (("with header", with_header), ("without header", without_header)):
        marker = _UNSET
        if response.status_code == 200:
            try:
                members = response.json().get("members") or []
            except ValueError:
                members = []
            if members:
                marker = str(members[0].get("type"))
        _p(f"    {label:<15} status={response.status_code}  members[0].type={marker}")
    _p("    (differing `type` values mean the appliance silently served an older schema)")


async def _probe_profiles(client: OneViewClient, page_size: int) -> list[dict[str, Any]]:
    """
    Enumerate the server profiles and report whether paging is complete.

    Open question 1 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        client (OneViewClient): A logged-in client.
        page_size (int): The `count` to send.

    Returns:
        list[dict[str, Any]]: Every profile the appliance returned.
    """
    _header("2. Server profiles — the 256 ceiling")
    first = await client.raw_get(f"/rest/server-profiles?start=0&count={page_size}")
    body = first.json() if first.status_code == 200 else {}
    total = body.get("total")
    members = body.get("members") or []
    next_uri = body.get("nextPageUri")
    _p(f"  requested count={page_size}")
    _p(f"  total reported by the appliance : {_present(total)}")
    _p(f"  members in the first page       : {len(members)}")
    _p(f"  nextPageUri                     : {_present(next_uri)}")

    profiles = await client.get_all("/rest/server-profiles", page_size=page_size)
    _p(f"  fetched by following nextPageUri: {len(profiles)}")
    _p()
    _p("  OPEN QUESTION 1 — is the 256 cap per request or per query:")
    if not isinstance(total, int):
        _p("    INCONCLUSIVE: the appliance reported no `total`.")
    elif total <= 256:
        _p(f"    INCONCLUSIVE: this appliance has only {total} profiles. Re-run where >256 exist.")
    elif len(profiles) >= total:
        _p("    PER REQUEST — paging works. Every profile was fetched.")
    else:
        _p(
            f"    PER QUERY — TRUNCATED. Only {len(profiles)} of {total} profiles are "
            "reachable through this endpoint, so this collector cannot see the whole "
            "estate and must shard by `filter`. This is a blocker; report it."
        )
    return profiles


def _report_generations(hardware: list[dict[str, Any]]) -> None:
    """
    Report the estate's iLO/model mix.

    `mpModel`'s real value set — open question 3 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        hardware (list[dict[str, Any]]): Every server-hardware member.
    """
    _header("3. iLO generations and models (open question 3)")
    mix: Counter[str] = Counter()
    for member in hardware:
        mix[
            "|".join(
                (
                    str(member.get("mpModel")),
                    str(member.get("mpFirmwareVersion")),
                    str(member.get("generation")),
                    str(member.get("shortModel")),
                )
            )
        ] += 1
    _p(f"  {'mpModel':<12} {'mpFirmwareVersion':<22} {'generation':<12} {'shortModel':<22} count")
    for combination, count in mix.most_common():
        model, firmware, generation, short = combination.split("|")
        _p(f"  {model:<12} {firmware:<22} {generation:<12} {short:<22} {count}")
    _p()
    _p("  Parsed generation (what the mapping derives from mpModel):")
    parsed: Counter[str] = Counter(
        str(ilo_generation(member.get("mpModel"))) for member in hardware
    )
    for value, count in parsed.most_common():
        _p(f"    iLO {value}: {count}")
    if "None" in parsed:
        _p("    'None' means mpModel carried no trailing digit — report the raw value above.")


def _report_ilo4_fidelity(hardware: list[dict[str, Any]]) -> None:
    """
    Report which hardware fields populate, split by iLO generation.

    Open question 2 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        hardware (list[dict[str, Any]]): Every server-hardware member.
    """
    _header("4. Which fields populate, by iLO generation (open question 2)")
    fields = ("memoryMb", "processorCount", "processorCoreCount", "processorType", "portMap")
    populated: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    for member in hardware:
        generation = f"iLO {ilo_generation(member.get('mpModel'))}"
        totals[generation] += 1
        tally = populated.setdefault(generation, Counter())
        for field in fields:
            if _present(member.get(field)) != _UNSET:
                tally[field] += 1
        for name in (DEVICES, LOCAL_STORAGE, LOCAL_STORAGE_V2):
            state = subresource(member, name).get("collectionState") or "absent"
            tally[f"{name}={state}"] += 1

    for generation, count in sorted(totals.items()):
        _p(f"  {generation} — {count} server(s)")
        for key, value in sorted(populated[generation].items()):
            _p(f"      {key:<32} {value}/{count}")
    _p()
    _p(
        "  A generation whose top-level fields read 0/N gets identity only: no CPU, no\n"
        "  memory, no NICs. That is the number that decides whether OneView-only is\n"
        "  enough for the iLO-4 half of the estate."
    )


def _report_addresses(hardware: list[dict[str, Any]], sample: int) -> None:
    """
    Print `mpHostInfo` verbatim for a sample of servers.

    `mpIpAddresses` ordering and cardinality — open question 4 in
    docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        hardware (list[dict[str, Any]]): Every server-hardware member.
        sample (int): How many to print.
    """
    _header("5. Management-processor addresses (open question 4)")
    missing = 0
    for member in hardware[:sample]:
        info = member.get("mpHostInfo")
        chosen = management_processor_address(member)
        if chosen is None:
            missing += 1
        _p(f"  {member.get('name')}")
        _p(f"      mpHostInfo : {json.dumps(info)}")
        _p(f"      chosen     : {_present(chosen)}")
    without = sum(1 for m in hardware if management_processor_address(m) is None)
    _p()
    _p(f"  servers with no usable address, across all {len(hardware)}: {without}")
    if missing:
        _p("  (a dash above means every entry was link-local/SLAAC, or there were none)")


def _report_names(
    hardware: list[dict[str, Any]], profiles: list[dict[str, Any]], sample: int
) -> None:
    """
    Show the three competing names side by side, and the site each parses to.

    Only the profile carries the operator's name — docs/hpe-collectors.md.

    Args:
        hardware (list[dict[str, Any]]): Every server-hardware member.
        profiles (list[dict[str, Any]]): Every server profile.
        sample (int): How many to print.
    """
    _header("6. Names: hardware vs serverName vs profile (open question 6)")
    sites = site_catalog(get_settings().sites)
    by_uri = {p.uri: p for p in (profile_from(raw) for raw in profiles) if p is not None}
    unassigned = 0
    for member in hardware[:sample]:
        profile = by_uri.get(str(member.get("serverProfileUri") or ""))
        if profile is None:
            unassigned += 1
        name = profile.name if profile else None
        site = parse_site_code(name, sites) if name else None
        _p(f"  hardware.name : {_present(member.get('name'))}")
        _p(f"  serverName    : {_present(member.get('serverName'))}")
        _p(f"  profile.name  : {_present(name)}   -> site {site or 'none in name'}")
        _p()
    total_unassigned = sum(1 for m in hardware if not m.get("serverProfileUri"))
    _p(f"  server hardware with no profile, across all {len(hardware)}: {total_unassigned}")
    _p("  (those are skipped by the collector — they have no name it can use)")
    _p("  A populated `serverName` on a host with no HPE AMS settles open question 6.")


async def _report_gpus(client: OneViewClient, hardware: list[dict[str, Any]]) -> None:
    """
    Report every GPU found and whether the catalog knows its VRAM.

    Open questions 7 and 8 in docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        client (OneViewClient): A logged-in client.
        hardware (list[dict[str, Any]]): Every server-hardware member.
    """
    _header("7. GPUs and the catalog (open questions 7 and 8)")
    catalog = gpu_catalog(get_settings().gpu_models)
    found: Counter[str] = Counter()
    gpu_hardware: dict[str, Any] | None = None
    for member in hardware:
        for device in subresource_data(member, DEVICES) or ():
            if device.get("DeviceType") != "GPU":
                continue
            found[str(device.get("Name"))] += 1
            gpu_hardware = gpu_hardware or member

    if not found:
        _p("  No GPU reported by any server's Devices subresource.")
    for model, count in found.most_common():
        enriched = catalog.enrich({"model": model, "memory_bytes": None})
        vram = enriched.get("memory_bytes")
        verdict = (
            f"CATALOG HIT  -> {enriched['model']} ({vram // 1024**3} GiB)"
            if isinstance(vram, int)
            else "CATALOG MISS -> VRAM will be unknown; add it to INVENTORY_GPU_MODELS"
        )
        _p(f"  {count:>4}x {model!r}")
        _p(f"         {verdict}")

    if gpu_hardware is None:
        _p()
        _p("  OPEN QUESTION 7 not answerable: no GPU-bearing host in this sample.")
        return
    uri = str(gpu_hardware.get("uri") or "")
    response = await client.raw_get(f"{uri}/processors")
    _p()
    _p(f"  OPEN QUESTION 7 — GET {uri}/processors (status {response.status_code}):")
    if response.status_code != 200:
        _p("    unavailable; the Redfish `ProcessorType: GPU` path cannot be confirmed.")
        return
    body = response.json() if response.content else {}
    data = body.get("data")
    members = data.get("Members") if isinstance(data, dict) else data
    kinds = Counter(
        str(row.get("ProcessorType")) for row in (members or []) if isinstance(row, dict)
    )
    _p(f"    collectionState={body.get('collectionState')}  ProcessorType values={dict(kinds)}")


def _report_server_detail(
    client: OneViewClient,
    hardware: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    sample: int,
) -> None:
    """
    Print, per server, exactly what the mapping produces from it.

    The end-to-end check: iLO generation, the profile-derived name, and
    every hardware field as either a value or "not read".

    Args:
        client (OneViewClient): The client, for its endpoint.
        hardware (list[dict[str, Any]]): Every server-hardware member.
        profiles (list[dict[str, Any]]): Every server profile.
        sample (int): How many to print.
    """
    _header(f"8. Per-server mapping (first {sample})")
    by_uri = {p.uri: p for p in (profile_from(raw) for raw in profiles) if p is not None}
    shown = 0
    for member in hardware:
        profile = by_uri.get(str(member.get("serverProfileUri") or ""))
        if profile is None or shown >= sample:
            continue
        shown += 1
        mapped = server_from(hardware=member, profile=profile, manager_id="probe")
        memory = (
            f"{mapped.memory_total_bytes / 1024**3:.1f} GiB"
            if mapped.memory_total_bytes is not None
            else "not read"
        )
        _p(f"  [{shown}] {mapped.name}")
        _p(f"      mpModel/firmware : {member.get('mpModel')} / {member.get('mpFirmwareVersion')}")
        _p(f"      model/serial     : {mapped.model} / {mapped.serial}")
        _p(
            f"      cpu              : {mapped.cpu_sockets} sockets x "
            f"{member.get('processorCoreCount')} cores/socket = "
            f"{mapped.cpu_cores if mapped.cpu_cores is not None else 'not read'} cores"
        )
        _p(f"      memory           : {memory} (memoryMb={member.get('memoryMb')})")
        _p(f"      bmc              : {mapped.bmc_address_raw or 'not read'}")
        _p(
            f"      nics             : "
            f"{'not read' if mapped.nic_macs is None else f'{len(mapped.nic_macs)} mac(s)'}"
        )
        _p(
            f"      drives           : "
            f"{'not read' if mapped.storage_drives is None else len(mapped.storage_drives)}"
        )
        _p(f"      gpus             : {'not read' if mapped.gpus is None else len(mapped.gpus)}")
        _p(f"      profile template : {mapped.profile_template_name or _UNSET}")


def _report_subresource_shape(hardware: list[dict[str, Any]]) -> None:
    """
    Report whether `subResources` is an object or an array.

    So the mapping's dead branch can be deleted — open question 9 in
    docs/adr/0022-oneview-only-hpe-collector.md.

    Args:
        hardware (list[dict[str, Any]]): Every server-hardware member.
    """
    _header("9. subResources container shape")
    shapes = Counter(type(member.get("subResources")).__name__ for member in hardware)
    _p(f"  {dict(shapes)}")
    for member in hardware:
        holder = member.get("subResources")
        if holder:
            names = (
                sorted(holder) if isinstance(holder, dict) else [str(e.get("name")) for e in holder]
            )
            _p(f"  subresource names on {member.get('name')}: {names}")
            break


async def _run(argv: list[str] | None = None) -> int:
    """
    Probe the configured appliance.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        int: 0 when the appliance answered, 1 when it did not, 2 when
            nothing is configured.
    """
    args = _parse_args(argv)
    settings = get_settings()
    try:
        connection = EnvConnectionResolver(settings).resolve(ManagerType.ONEVIEW)
    except ManagerNotConfiguredError as exc:
        _p(str(exc))
        return 2

    _p()
    _p("=" * 72)
    _p(f"OneView appliance: {connection.endpoint}")
    _p("=" * 72)

    client = OneViewClient(
        endpoint=connection.endpoint,
        username=connection.username,
        password=connection.password,
        timeout_seconds=settings.collector_connect_timeout_seconds,
        api_version=settings.oneview_api_version,
        verify_tls=settings.oneview_verify_tls,
    )
    try:
        async with client:
            await _probe_version(client)
            profiles = await _probe_profiles(
                client, settings.oneview_page_size or DEFAULT_PAGE_SIZE
            )
            hardware = await client.get_all(
                "/rest/server-hardware", page_size=25, params={"expand": "all"}
            )
            _p(f"\n  server hardware fetched: {len(hardware)}")
            verdict = await _check_core_count(client, hardware)
            _report_generations(hardware)
            _report_ilo4_fidelity(hardware)
            _report_addresses(hardware, args.sample)
            _report_names(hardware, profiles, min(args.sample, 5))
            await _report_gpus(client, hardware)
            _report_server_detail(client, hardware, profiles, args.sample)
            _report_subresource_shape(hardware)
            await _report_power_supplies(client, hardware)
    except OneViewConnectionError as exc:
        _p(f"\n{connection.endpoint}: FAILED — {exc}")
        return 1

    _p()
    _p(f"  >>> {verdict}")
    _p()
    _p(
        "Record what this printed in docs/adr/0022-oneview-only-hpe-collector.md's\n"
        "validation section, and in docs/hpe-collectors.md for anything it settles."
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """
    Entry point.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.
    """
    raise SystemExit(asyncio.run(_run(argv)))


if __name__ == "__main__":
    main()
