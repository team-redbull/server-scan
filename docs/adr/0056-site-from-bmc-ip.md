# ADR-0056: A BMC IP prefix is the last fallback for a server's site

**Status:** Accepted, implemented 2026-11-04.

**Superseded in part by ADR-0057 (2026-11-05): the BMC IP now wins over the name.**

**Extends** ADR-0018 (sites from configuration) and ADR-0011 (a site is derived, never declared).

## Context

A server's site came from its name, then from the UCS org path (`profile_dn`). A server whose
name carries no site token and that has no profile (most Dell and HPE standalone hardware) stayed
"Unassigned". The operator's network plan is by site: every iDRAC under `10.` is Tel Aviv, `149.`
Bat Yam, `69.` Tokyo.

## Decision

`INVENTORY_SITE_BMC_PREFIXES` (Helm `config.siteBmcPrefixes`) is a list of `prefix=site` pairs. The
order is: name, then profile DN, then BMC IP. The IP is only read when the first two say nothing, so
a name that carries a site always wins.

- A prefix is one to four **whole octets** (`10`, `10.20`); `10` never matches `100.1.1.1`. The
  longest matching prefix wins.
- The site must be in `INVENTORY_SITES` (a code or an alias; stored as the canonical code). A
  malformed rule, a duplicate prefix or an unknown site fails startup, like `INVENTORY_SITES`.
- IPv4 only. A hostname BMC or an IPv6 address matches nothing (stays Unassigned).
- A run that could not read the BMC address reuses the stored `network.bmc.host`, so a transient read
  failure does not flip an IP-derived site to Unassigned.
- It is evaluated at ingest, so a changed rule applies on the next collector run.

## Consequences

- The domain still never reads `Settings`: the rules ride on the immutable `SiteCatalog`
  (`site_catalog(sites, bmc_prefixes)`); callers that build an ingest catalog pass both.
- A wrong prefix mislabels every server in that range, which a hostname-derived site would not.
  Mitigation: the name always wins, and the rule is explicit configuration.
