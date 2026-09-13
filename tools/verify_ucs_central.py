"""Read-only probe settling the one question `docs/adr/0014` could not.

**Does Central's `lsServer` include each domain's locally-defined service
profiles, or only the global ones Central itself owns**? That question
decides whether the UCS Central collector works at all. A
UCS server's name comes from its service profile — `computeBlade.name` is
empty in practice (`docs/adr/0009`) — and the name is what carries the
site token, the classification pattern, and the
`INVENTORY_COLLECTOR_NAME_PATTERN` match. If Central holds only global
profiles, every server under a local profile arrives named after its
chassis slot and is silently dropped.

Writes nothing: no MongoDB connection, no ingest pipeline, no `Manager`
document. It logs in, runs read-only queries, prints a verdict, logs out.

    uv run python -m tools.verify_ucs_central

Reads the same `INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD` the
collector does, so if this works the collector can connect too.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections import Counter
from typing import Any

from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.providers.ucs_central.client import UcsCentralClient
from app.infrastructure.providers.ucs_central.provider import domain_id_from_dn
from app.infrastructure.providers.ucs_common import (
    TEMPLATE_TYPES,
    is_equipped,
    normalize_oper_state,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse this CLI's arguments.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed `--show-names` value.
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
    return parser.parse_args(argv)


def _p(text: str = "") -> None:
    """
    Print one line, flushed immediately.

    Flushed so this script's output interleaves correctly when run inside
    a container or piped through `tee`.

    Args:
        text (str): The line to print; defaults to a blank line.
    """
    print(text, flush=True)


def _header(text: str) -> None:
    """
    Print a blank line, `text`, and an underline of `=` matching its width.

    Args:
        text (str): The section heading to print.
    """
    _p()
    _p(text)
    _p("=" * len(text))


async def _run(show_names: int) -> int:
    """
    Log into UCS Central, run the read-only probe queries, and print a verdict.

    Args:
        show_names (int): Print this many resolved server names as a spot
            check; `0` to disable.

    Returns:
        int: Exit code — 0 every server resolved a name (GOOD), 1
            inconclusive/bad/partial, 2 not configured.
    """
    settings = get_settings()
    try:
        connection = EnvConnectionResolver(settings).resolve(ManagerType.UCS_CENTRAL)
    except ManagerNotConfiguredError as exc:
        _p(str(exc))
        return 2

    client = UcsCentralClient(
        endpoint=connection.endpoint,
        username=connection.username,
        password=connection.password,
        timeout_seconds=max(settings.collector_connect_timeout_seconds, 60.0),
    )

    _p(f"Connecting to UCS Central at {connection.endpoint} as {connection.username} ...")
    await client.login()
    try:
        domains = await client.query_classid("computeSystem")
        blades = await client.query_classid("computeBlade")
        racks = await client.query_classid("computeRackUnit")
        ls_servers = await client.query_classid("lsServer")
        # The decisive query: `ownership_state`, vocabulary in ADR-0014.
        sp_meta = await client.query_classid("lsSPMeta")
        inventory_eps = await client.query_classid("inventoryDomainEp")
        disk_units = await client.query_classid("storageLocalDisk")
        ext_eth_ifs = await client.query_classid("adaptorExtEthIf")
        host_eth_ifs = await client.query_classid("adaptorHostEthIf")
        top_systems = await client.query_classid("topSystem")
    finally:
        await client.logout()

    servers = [mo for mo in (*blades, *racks) if is_equipped(mo)]
    profiles = {
        mo.dn: mo for mo in ls_servers if str(getattr(mo, "type", "") or "") not in TEMPLATE_TYPES
    }
    templates = [mo for mo in ls_servers if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES]

    _header("1. Registered domains")
    sync_by_sys = {str(getattr(m, "sys_id", "") or ""): m for m in inventory_eps}
    _p(f"{'ID':<8} {'NAME':<26} {'ADDRESS':<16} {'INV STATUS':<14} {'REPORTED':>8} {'SEEN':>6}")
    seen_by_domain = Counter(
        did for mo in servers if (did := domain_id_from_dn(getattr(mo, "dn", ""))) is not None
    )
    for d in sorted(domains, key=lambda m: str(getattr(m, "name", ""))):
        did = str(getattr(d, "id", "") or "")
        _p(
            f"{did:<8} "
            f"{getattr(d, 'name', '') or '—'!s:<26} "
            f"{getattr(d, 'address', '') or '—'!s:<16} "
            f"{getattr(d, 'inventory_status', '') or '—'!s:<14} "
            f"{getattr(d, 'total_physical_cnt', '') or '?'!s:>8} "
            f"{seen_by_domain.get(did, 0):>6}"
        )
        sync = sync_by_sys.get(did)
        if sync is not None:
            _p(f"{'':<8} last inventory update: {getattr(sync, 'latest_update_time', '—')}")
    _p()
    _p(f"{len(domains)} domain(s), {len(servers)} equipped server(s) collected across all of them.")
    domain_name_by_id = {
        str(getattr(d, "id", "") or ""): str(getattr(d, "name", "") or "—") for d in domains
    }

    _header("2. THE DECISIVE QUESTION — are local service profiles present?")
    ownership = Counter(str(getattr(m, "ownership_state", "") or "?") for m in sp_meta)
    _p(
        f"lsServer objects returned : {len(ls_servers)}  "
        f"({len(profiles)} profiles, {len(templates)} templates)"
    )
    _p(f"lsSPMeta objects returned : {len(sp_meta)}")
    _p()
    if ownership:
        _p("ownership_state breakdown:")
        for state, count in ownership.most_common():
            note = {
                "localized": "  <-- owned by its own domain (a LOCAL profile)",
                "global-controlled": "  <-- owned by UCS Central (a GLOBAL profile)",
            }.get(state, "")
            _p(f"  {state:<22} {count:>6}{note}")
    else:
        _p("No lsSPMeta objects returned at all.")

    _header("3. Do servers actually resolve a name?")
    resolved = 0
    unresolved_by_domain: Counter[str] = Counter()
    names: list[str] = []
    for mo in servers:
        profile = profiles.get(getattr(mo, "assigned_to_dn", None) or "")
        name = getattr(profile, "name", None) if profile is not None else None
        if name:
            resolved += 1
            names.append(str(name))
        else:
            unresolved_by_domain[domain_id_from_dn(getattr(mo, "dn", "")) or "?"] += 1

    _p(f"servers with a resolved service-profile name : {resolved} / {len(servers)}")
    if unresolved_by_domain:
        _p("servers with NO name, by domain:")
        for did, count in unresolved_by_domain.most_common():
            _p(f"  domain {did:<10} {count:>6}")

    pattern = settings.collector_name_pattern
    if pattern:
        matching = [n for n in names if re.search(pattern, n)]
        _p()
        _p(f"names matching INVENTORY_COLLECTOR_NAME_PATTERN={pattern!r}: {len(matching)}")
    if show_names and names:
        _p()
        _p(f"first {min(show_names, len(names))} resolved name(s):")
        for n in names[:show_names]:
            _p(f"  {n}")

    _report_disk_health_vocabulary(disk_units)
    _report_operstate_vocabulary(ext_eth_ifs, host_eth_ifs)
    _report_fabric_name(top_systems, domain_name_by_id)

    _header("VERDICT")
    localized = ownership.get("localized", 0)
    if not servers:
        _p("INCONCLUSIVE — Central returned no equipped servers at all. Check that domains are")
        _p("registered and their inventory has synced (section 1).")
        return 1
    if resolved == len(servers):
        _p("GOOD — every collected server resolved a service-profile name.")
        if localized:
            _p(f"       {localized} profile(s) are 'localized', so Central DOES replicate")
            _p("       domain-local service profiles. ADR-0014's open question is settled: yes.")
        else:
            _p("       Note: no 'localized' profiles exist here, so this fleet does not exercise")
            _p("       the local-profile case. It works, but the question stays open for a fleet")
            _p("       that does use local profiles.")
        _p("       The UCS Central collector is safe to run for this fleet.")
        return 0
    if resolved == 0:
        _p("BAD — no server resolved a name. Every one would fall back to a chassis-slot DN,")
        _p("      carry no site token, and be dropped by INVENTORY_COLLECTOR_NAME_PATTERN.")
        _p("      Collect these domains through their own UCS Manager instead.")
        return 1
    _p(f"PARTIAL — {resolved} of {len(servers)} servers resolved a name.")
    _p("          The domains listed in section 3 would lose their servers entirely.")
    _p("          Collect those domains through their own UCS Manager.")
    return 1


def _mapped_disk_health(disk_state: str) -> str:
    """
    A local mirror of `ucs_manager.mapping._disk_health`'s `_DISK_HEALTH_MAP`, for reporting only.

    Args:
        disk_state (str): The raw `disk_state` value, already lower-cased.

    Returns:
        str: HEALTHY, WARNING, CRITICAL, or UNKNOWN.
    """
    healthy = {
        "good",
        "online",
        "unconfigured-good",
        "global-hot-spare",
        "dedicated-hot-spare",
        "jbod",
    }
    warning = {
        "predictive-failure",
        "rebuilding",
        "copyback",
        "foreign-configuration",
        "locked-foreign-configuration",
    }
    critical = {"bad", "failed", "unconfigured-bad", "disabled-for-removal"}
    if disk_state in healthy:
        return "HEALTHY"
    if disk_state in warning:
        return "WARNING"
    if disk_state in critical:
        return "CRITICAL"
    return "UNKNOWN"


def _report_disk_health_vocabulary(disk_units: list[Any]) -> None:
    """
    Cross-check every raw `disk_state` value this domain set reports against `_DISK_HEALTH_MAP`.

    See ADR-0009's "Update (2026-09-07): the health/oper vocabulary gaps...".

    Args:
        disk_units (list[Any]): Every `storageLocalDisk` MO returned by
            the domain-wide query.
    """
    _header("4. DISK HEALTH VOCABULARY — why some drives read health=UNKNOWN")

    counts: Counter[str] = Counter(
        str(getattr(mo, "disk_state", "") or "").lower() for mo in disk_units
    )
    if not counts:
        _p("no storageLocalDisk MOs returned.")
        return

    unknown_total = 0
    _p(f"{'disk_state':<26}{'count':>7}  mapped")
    for state, n in counts.most_common():
        mapped = _mapped_disk_health(state)
        flag = "  <- not recognized" if mapped == "UNKNOWN" else ""
        _p(f"{state or '(empty)':<26}{n:>7}  -> {mapped}{flag}")
        if mapped == "UNKNOWN":
            unknown_total += n

    _p(f"\n{unknown_total} of {sum(counts.values())} disk(s) read health=UNKNOWN.")
    if unknown_total:
        _p("A non-empty state above that still maps to UNKNOWN is a real spelling gap —")
        _p("add it to `_DISK_HEALTH_MAP` in `ucs_manager/mapping.py` (`ucs_central` reuses")
        _p("the same UcsManagerProvider per domain, so one fix covers both). An empty state")
        _p("usually means an unequipped slot with no disk in it.")


def _report_operstate_vocabulary(ext_eth_ifs: list[Any], host_eth_ifs: list[Any]) -> None:
    """
    Cross-check every raw `oper_state` value this domain set reports against `normalize_oper_state`.

    See ADR-0009's "Update (2026-09-07): the health/oper vocabulary gaps...".

    Args:
        ext_eth_ifs (list[Any]): Every `adaptorExtEthIf` MO (physical
            uplinks) returned by the domain-wide query.
        host_eth_ifs (list[Any]): Every `adaptorHostEthIf` MO (vNICs)
            returned by the domain-wide query.
    """
    _header("5. OperState VOCABULARY — why some interfaces read oper=UNKNOWN")

    classes = {"adaptorExtEthIf (physical)": ext_eth_ifs, "adaptorHostEthIf (vNIC)": host_eth_ifs}
    any_unrecognized = False
    for label, mos in classes.items():
        counts: Counter[str] = Counter(str(getattr(mo, "oper_state", "") or "") for mo in mos)
        if not counts:
            continue
        _p(f"\n{label}:")
        for raw, n in counts.most_common():
            mapped = normalize_oper_state(raw)
            flag = "  <- not recognized" if mapped == "UNKNOWN" else ""
            _p(f"  {raw or '(empty)'!r:<20} x{n:<6} -> {mapped}{flag}")
            if flag:
                any_unrecognized = True

    _p()
    if any_unrecognized:
        _p("A non-empty value above that still maps to UNKNOWN is a real spelling gap — add")
        _p("it to `ucs_common._OPER_STATE_MAP` with the UP/DOWN/DISABLED it actually means.")
    else:
        _p("Every raw oper_state value observed maps to something other than UNKNOWN.")


def _report_fabric_name(top_systems: list[Any], domain_name_by_id: dict[str, str]) -> None:
    """
    Report what `topSystem.name` (now `ProviderAttachment.fabric_name`) looks like per domain.

    Args:
        top_systems (list[Any]): Every `topSystem` MO returned by the
            domain-wide query — one per registered domain.
        domain_name_by_id (dict[str, str]): Domain id -> the name
            Central already shows for it in section 1
            (`computeSystem.name`), for comparison.
    """
    _header("6. FABRIC INTERCONNECT NAME — topSystem.name, now wired into fabric_name")

    if not top_systems:
        _p("no topSystem MOs returned — cannot preview a fabric_name value.")
        return

    _p("fabric_name now carries topSystem.name — the domain's own shared cluster name,")
    _p("identical for FI-A and FI-B of one domain (UCS Manager has no per-FI hostname).")
    _p('Confirmed live 2026-09-07; see ADR-0009\'s "fabric_name built and confirmed live".')
    _p()
    _p(f"{'domain':<10}{'topSystem.name':<28}{'topSystem.address':<18}{'Central calls it':<26}")
    for mo in sorted(top_systems, key=lambda m: str(getattr(m, "name", ""))):
        did = domain_id_from_dn(getattr(mo, "dn", "")) or "?"
        name = str(getattr(mo, "name", "") or "—")
        address = str(getattr(mo, "address", "") or "—")
        central_name = domain_name_by_id.get(did, "—")
        _p(f"{did:<10}{name:<28}{address:<18}{central_name:<26}")


def main(argv: list[str] | None = None) -> None:
    """
    Entry point: parse args, run the probe, and exit with its verdict code.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Raises:
        SystemExit: With `_run`'s exit code.
    """
    args = _parse_args(argv)
    if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
        _p("(INVENTORY_UCS_DUMP_XML=1 — raw XML will be dumped)")
    raise SystemExit(asyncio.run(_run(args.show_names)))


if __name__ == "__main__":
    main()
