"""Centralized application configuration.

All environment lookups go through this module — never scatter `os.environ`
calls through the codebase. Settings are loaded once at startup and injected
via FastAPI dependencies (see `app.main`), not imported as a bare module-level
singleton, so tests can override them cleanly with `dependency_overrides`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_DEV_CURSOR_SECRET = "dev-insecure-cursor-secret-change-in-production"  # noqa: S105 - dev default, not a real secret


class Settings(BaseSettings):
    """
    Application settings, sourced from environment variables and an optional `.env` file.

    Every field's environment variable is prefixed `INVENTORY_`; see
    `.env.example` for the full list with explanations.
    """

    model_config = SettingsConfigDict(
        env_prefix="INVENTORY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity ---
    service_name: str = "server-scan-api"
    environment: Literal["development", "test", "staging", "production"] = "development"

    # --- HTTP server ---
    # Binding all interfaces is correct here, not a hardening gap: this is
    # the address the process listens on *inside* its own container, where
    # 127.0.0.1 would make it unreachable from the pod network entirely.
    # The actual exposure boundary is the container/Service network policy,
    # not this value.
    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8080
    # NoDecode: without it, pydantic-settings tries to JSON-decode any
    # list-typed env var before our validator runs, so a plain comma-
    # separated value like "http://a,http://b" fails with a JSONDecodeError
    # instead of reaching `_split_csv` below.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # --- MongoDB ---
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "server-scan"
    mongo_connect_timeout_ms: int = 5_000
    mongo_server_selection_timeout_ms: int = 5_000
    mongo_socket_timeout_ms: int = 10_000
    mongo_max_pool_size: int = 100
    mongo_min_pool_size: int = 0

    # --- Redis ---
    redis_uri: str = "redis://localhost:6379/0"
    redis_connect_timeout_seconds: float = 2.0
    redis_socket_timeout_seconds: float = 2.0
    redis_max_connections: int = 50
    cache_default_ttl_seconds: int = 300

    # --- Pagination / search limits ---
    default_page_size: int = 50
    max_page_size: int = 200
    # Signs opaque keyset-pagination cursors (see `app.domain.services.cursor`)
    # so a client can never forge one that skips the filter/sort binding
    # check. Insecure default is fine for dev/test; production deployments
    # must override via INVENTORY_CURSOR_SECRET.
    cursor_secret: str = _INSECURE_DEV_CURSOR_SECRET

    # --- Sites ---
    #
    # Which sites this deployment has, as `code:Display Name` pairs:
    #
    #     INVENTORY_SITES="nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam"
    #
    # The code is the token that appears inside a hostname
    # (`ocp4-prod-tlv-infra-01` -> `tlv`), so it must be lowercase
    # letters, digits and single hyphens. The display half is optional;
    # `nyc,tlv` gives title-cased names. The code half may itself be
    # `|`-separated aliases (`znif|prep:Znif`) when more than one naming
    # convention names the same site — the first token is canonical, the
    # rest are recognised but never produced (`Server.site_id`, every URL,
    # always the first token). See `SiteCatalog.from_spec`.
    #
    # Configuration rather than an enum in the source, because which
    # sites exist is a property of one estate's naming convention — see
    # docs/adr/0018-sites-from-configuration.md. Empty uses the shipped
    # default, so dev and CI need set nothing.
    sites: str = ""

    # --- GPU model catalog ---
    #
    # Field name matches the env var suffix exactly (`gpu_models` ->
    # `INVENTORY_GPU_MODELS`), the same convention `sites` -> `INVENTORY_SITES`
    # already uses — pydantic-settings derives the env var name from the
    # field name with no alias, so the two must agree letter for letter or
    # the configured value is silently ignored (`extra="ignore"` above
    # means an unrecognized env var never raises). This field was
    # originally named `gpu_model_catalog`, which pydantic-settings read
    # as `INVENTORY_GPU_MODEL_CATALOG` — a name nothing else in this repo
    # (`.env.example`, the Helm chart, this file's own docstring above)
    # ever documented or set, so `INVENTORY_GPU_MODELS` was silently a
    # no-op until this was renamed. Confirmed live, not just by reading:
    # `Settings()` genuinely returned the `INVENTORY_GPU_MODEL_CATALOG`
    # value and ignored `INVENTORY_GPU_MODELS` before this fix.
    #
    # No management plane this platform collects from reports a GPU's
    # memory size. The platform ships a built-in table instead
    # (`app.domain.value_objects.gpu_models`), so empty is not "enrich
    # nothing" — it is the built-in table alone. This value **overrides**
    # that table, as `PID:Friendly Name:VRAM_GB` triples,
    # comma-separated:
    #
    #     INVENTORY_GPU_MODELS="P1001-200:NVIDIA A100 40GB:40,P1010-200:NVIDIA H100 80GB:80"
    #
    # The first field is a Cisco PID or a vendor model string; both
    # match. See docs/adr/0021-built-in-gpu-catalog-with-model-matching.md
    # for why the "deliberately not a hardcoded table" decision this
    # comment used to record was reversed.
    gpu_models: str = ""

    # A network interface's OS-level name (`ens8f0np0`) against the
    # hardware name a BMC reports (`NIC.Slot.8-1-1`). Neither is derivable
    # from the other and no management API reports the first, so this is
    # stated knowledge, learned by booting a host and reading `ip link`:
    #
    #     INVENTORY_NIC_OS_NAMES="Slot.8=ens8f0np0,ens8f1np1;Integrated.1=eno12399np0,eno12409np1"
    #
    # Keyed on the FQDD kind and ordered by port, so one entry covers
    # every server with that card in that slot regardless of hostname.
    # Empty is a real state: the UI shows the hardware name alone rather
    # than inventing one. See `app.domain.value_objects.nic_names`.
    nic_os_names: str = ""

    # --- Regex / classification safety ---
    regex_max_pattern_length: int = 200
    regex_match_timeout_seconds: float = 0.25

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Metrics ---
    metrics_enabled: bool = True
    # A server not successfully collected for this long counts toward
    # `server_scan_servers_stale`. Default is two of the shared 6-hour
    # collector cycles (ADR-0029).
    stale_after_seconds: int = 43200
    # Shortest gap between two MongoDB fleet-snapshot queries; scrapes in
    # between serve the last values.
    metrics_fleet_refresh_seconds: float = 30.0

    # --- Collectors (tools/run_collector.py, not the API process) ---
    #
    # A credential pair per manager type, plus an endpoint for every type
    # that is pointed at directly — and that is the whole of a collector's
    # connection config. There is no per-manager document to maintain and
    # no secret directory to mount. Every value is empty by default so a
    # collector for an unconfigured vendor fails with an explicit "not
    # configured" error naming the variables to set, rather than
    # attempting a connection to nowhere.
    #
    # Cisco UCS Manager is the one type with a login but **no endpoint**.
    # It is never pointed at directly: the UCS Central collector reaches
    # every registered domain in turn, at the address Central reports for
    # each (`ComputeSystem.address`), using this one fleet-wide service
    # account. An `ucs_manager_ip` would have to name a single domain,
    # which would be either unused or actively misleading — see
    # `app.infrastructure.credentials.env`, where the split between
    # "has a login" and "has an endpoint" is a difference of type.
    #
    # `INVENTORY_`-prefixed, so `ucs_manager_ip` is
    # `INVENTORY_UCS_MANAGER_IP`. In Kubernetes these arrive from a
    # Secret via `envFrom` — see `deploy/helm/server-scan/values.yaml`.
    #
    # Cisco Intersight does not have a login at all, and its settings say
    # so rather than reusing the username/password shape every other
    # vendor here uses: it signs each request with an API key, so it needs
    # a key id and a PEM, and calling those a username and a password
    # made an operator's first guess — an account password — look
    # plausible. `ip` is `intersight.com` for the SaaS tenant, or the
    # appliance FQDN for an on-prem appliance.
    ucs_manager_username: str = ""
    ucs_manager_password: SecretStr = SecretStr("")

    ucs_central_ip: str = ""
    ucs_central_username: str = ""
    ucs_central_password: SecretStr = SecretStr("")

    # One appliance, exactly like every other vendor here. HPE caps an
    # appliance at 2500 servers (1024 off ESXi), which this estate is
    # well inside; a larger one would need a second endpoint, and
    # docs/hpe-collectors.md records that rather than the code
    # anticipating it.
    oneview_ip: str = ""
    oneview_username: str = ""
    oneview_password: SecretStr = SecretStr("")

    # `X-Api-Version`, which OneView requires on every call. 0 discovers
    # it from each appliance's unauthenticated `GET /rest/version` and
    # clamps it to the newest version this mapping was written against
    # (`..providers.oneview.client.MAX_TESTED_API_VERSION`). Set a number
    # only to pin or roll back after an appliance upgrade moves a field.
    oneview_api_version: int = 0

    # Off for the same reason `ome_bmc_verify_tls` is: an appliance in an
    # air-gapped estate ships a self-signed certificate. Turn it on where
    # a trusted chain exists — that is the scalable answer.
    oneview_verify_tls: bool = False

    # Explicit `count` on every collection GET. 256 is the documented
    # hard ceiling on /rest/server-profiles, and `count=-1` there means
    # *64*, not "all" — which is the single most likely way to ship a
    # collector that silently sees a fraction of the estate.
    oneview_page_size: int = 256

    # Power supplies are collected the cheap way first — many servers'
    # `expand=all` response already carries them (confirmed on a live
    # appliance 2026-09-07: 688 of 821), and only the rest cost a
    # per-server call. On by default anyway: no provider had ever
    # populated `ProviderServer.psus` before OneView shipped, while the
    # health engine has carried `power.psu_count`/`power.failed_psu_count`
    # the whole time, and OneView reports a PSU's state precisely enough
    # to feed them (`Failed`/`Degraded`/`ACPowerLost`). Turn it off for an
    # appliance that struggles with the fan-out; every server whose
    # `expand=all` didn't already cover it then reads `psus` as unread and
    # ingest carries the stored value forward.
    oneview_collect_psus: bool = True

    # How many of those per-server calls run at once. HPE documents no
    # rate limit for OneView at all, and one appliance is a single point
    # of failure for up to 2500 servers, so this stays conservative —
    # ADR-0016's "embedded management hardware degrades when polled"
    # warning applies to the appliance too.
    oneview_psu_concurrency: int = 8

    # Same shape as `oneview_collect_psus`, for `/processors` instead of
    # `/powerSupplies` — the only source `cpu_threads` has, since
    # `server-hardware`'s own fields carry `processorCount`/
    # `processorCoreCount` but no thread count. Also tried the cheap way
    # first via `expand=all` before falling back to a per-server call.
    oneview_collect_cpu_threads: bool = True
    oneview_cpu_threads_concurrency: int = 8

    ome_ip: str = ""
    ome_username: str = ""
    ome_password: SecretStr = SecretStr("")

    # The Dell collector needs TWO logins, and that is a deliberate
    # exception to "one endpoint and one login per manager type": OME is
    # asked only who exists, and each server's own iDRAC is asked what it
    # is. One shared read-only iDRAC account for the estate, as
    # `INVENTORY_OME_BMC_USERNAME`/`_PASSWORD`. See
    # docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md.
    ome_bmc_username: str = ""
    ome_bmc_password: SecretStr = SecretStr("")
    ome_bmc_port: int = 443

    # iDRACs ship a factory self-signed certificate, so verification is off
    # by default here where the standalone Redfish collector leaves it on:
    # that collector's fleet is an operator-written file that can name a CA
    # bundle per host, while this one's is whatever OME reports. Set
    # `redfish_ca_bundle` and turn this on for a fleet with a real internal
    # CA — that is the scalable answer, not leaving verification off.
    ome_bmc_verify_tls: bool = False

    # Everything else about talking to a BMC — timeouts, budgets, fleet
    # concurrency, the auth-failure guard, TLS floor, CA bundle — is
    # deliberately shared with the standalone Redfish collector's
    # `redfish_*` settings below rather than duplicated with an `ome_`
    # prefix. It is the same protocol against the same class of device, and
    # two sets of knobs would drift.

    intersight_ip: str = ""
    # The API Key ID exactly as Intersight shows it beside the key: a
    # `/`-joined string, not a username.
    intersight_api_key_id: str = ""
    # That key's PEM private half, unencrypted. Multi-line, and it rides
    # in the environment like any other value — there is no key file to
    # mount, because the signer accepts the key as a string. See
    # docs/adr/0017-intersight-collector.md, "Decision 2".
    intersight_api_key_pem: SecretStr = SecretStr("")

    # Which `ManagementMode` values the Intersight collector ingests, as
    # a comma-separated list. `UCSM` is excluded by default and that is
    # the whole point: those servers are exactly the ones the UCS Central
    # collector already owns, and collecting both makes one document's
    # fields flip on whichever CronJob ran last. Add `UCSM` only for an
    # estate whose UCS domains are not registered with Central at all.
    intersight_management_modes: str = "Intersight,IntersightStandalone"

    # Deliberately no `intersight_ca_bundle` / `intersight_tls_verify`
    # setting: `IntersightClient` never verifies the endpoint's TLS
    # certificate, unconditionally, by explicit user decision. There is
    # no environment variable that changes this. See
    # `app.infrastructure.providers.intersight.client.IntersightClient`.

    # `$top`. 1000 is the API's documented maximum and the default
    # because every query here is a fleet-wide list — lower it only if a
    # tenant turns out to throttle, since Cisco publishes no rate limit.
    intersight_page_size: int = 1000

    # A fleet-wide page is a large response, so reading one is bounded
    # separately from establishing the connection — the same split, for
    # the same reason, as the Redfish collector's two timeouts.
    intersight_read_timeout_seconds: float = 60.0

    # Wall clock for one run, enforced in-process so a throttled run ends
    # with a summary naming what it never reached. A hard kill by the
    # CronJob's `activeDeadlineSeconds` reports nothing.
    intersight_run_budget_seconds: float = 1800.0

    collector_connect_timeout_seconds: float = 15.0

    # --- Standalone Redfish collector ---
    #
    # A fleet-wide fallback login and **no endpoint**, the same shape
    # UCS_MANAGER has: the endpoints are the hosts in the inventory file.
    # Most estates give each BMC its own account, so the inventory's own
    # per-host credential names are the normal path and these two are the
    # fallback. See docs/adr/0016-redfish-standalone-collector.md.
    redfish_username: str = ""
    redfish_password: SecretStr = SecretStr("")

    # TOML, mounted read-only from a ConfigMap. Accepts a file or a
    # directory of `*.toml` — a directory is what lets a large estate be
    # sharded per site without a format change.
    redfish_inventory_file: str = ""
    # TOML, mounted read-only from a Secret, mapping credential names to
    # username/password. Optional: a fleet on one account needs only the
    # two variables above.
    redfish_credentials_file: str = ""

    # PEM bundle trusted in addition to the system store. The scalable
    # answer for self-signed BMC certificates — Dell's custom signing
    # certificate, or an internal CA imported to every BMC. Empty uses the
    # system trust store alone, which correctly rejects a factory
    # self-signed certificate.
    redfish_ca_bundle: str = ""
    redfish_tls_min_version: Literal["TLSv1", "TLSv1_1", "TLSv1_2", "TLSv1_3"] = "TLSv1_2"

    # Connect and read are split because they answer different questions:
    # connect bounds "is this host there at all" and wants to fail fast
    # across a fleet with dead hosts in it, while read bounds a BMC that
    # answered but is slow. 30s for read is evidence-led — a documented
    # iLO fleet failed at 3s and was fixed at 20s.
    redfish_connect_timeout_seconds: float = 10.0
    redfish_read_timeout_seconds: float = 30.0

    # Total wall clock for one host, all requests. Neither timeout above
    # can bound this: a BMC that answers every packet slowly consumes
    # unbounded time without ever tripping a socket timeout.
    redfish_host_budget_seconds: float = 180.0
    # Total wall clock for the run, enforced in-process so it trips before
    # the CronJob's `activeDeadlineSeconds` and can report what it
    # collected. A hard kill reports nothing.
    redfish_run_budget_seconds: float = 3600.0

    # BMCs read at once. They are independent devices, so this is bounded
    # by our own sockets and by how much management traffic the network
    # tolerates, not by any one BMC.
    redfish_fleet_concurrency: int = 16

    # How many domains the UCS Central collector talks to at once. It uses
    # Central only to enumerate registered domains and their service-profile
    # names, then reads each domain's real inventory from that domain's own
    # UCS Manager — so it needs INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD
    # (one login valid across the fleet; the addresses come from Central,
    # so INVENTORY_UCS_MANAGER_IP is not used by this collector).
    #
    # Domains are independent, and each costs a login plus 13 queries no
    # matter how many servers it holds (docs/adr/0014-ucs-central-multi-
    # domain-collector.md), so this bounds wall-clock without bounding
    # correctness. Kept modest: every concurrent domain is one blocking
    # SDK call parked in a worker thread.
    ucs_central_domain_concurrency: int = 4

    # No `ome_inventory_concurrency`: the Dell collector's expensive pass is
    # per-server against each BMC, not per-device against the appliance, and
    # it is bounded by `redfish_fleet_concurrency` with every other BMC
    # knob. See docs/adr/0020.

    # Which servers a collector is allowed to ingest at all, as a regex
    # matched against the server's name (`re.search`, so "starts with" is
    # spelled `^ocp`). A vendor manager holds the whole datacenter, not
    # just this platform's fleet, and there is no other way to tell the
    # two apart — the name is the only thing that carries the
    # distinction, which is already true of site parsing and
    # classification.
    #
    # Empty means collect everything, because the alternative — a
    # built-in default pattern — silently drops servers for anyone whose
    # naming differs, with an empty inventory as the only symptom. Set it
    # explicitly per deployment; `.env.example` and `values.yaml` both
    # ship `^ocp`.
    #
    # This is a *collection* filter, not a classification one: a
    # non-matching server is never fetched into MongoDB, so it has no
    # document, no health state and no audit trail. Deciding UPI vs.
    # hosted *within* the collected fleet is the classification engine's
    # job (`app.domain.services.classification`), not this.
    collector_name_pattern: str = ""

    # Per-collector overrides of `collector_name_pattern`, keyed on manager
    # type because that is how CronJobs, credentials and provider factories
    # are already partitioned. `None` inherits the global; an explicitly-set
    # empty string is the only way a collector opts *out* of a non-empty
    # global, which is why these are optional rather than plain `str`.
    # Resolved in one place — `tools.run_collector.resolve_name_pattern`.
    ucs_central_name_pattern: str | None = None
    intersight_name_pattern: str | None = None
    ome_name_pattern: str | None = None
    oneview_name_pattern: str | None = None
    redfish_name_pattern: str | None = None

    # --- OpenShift membership jobs (tools/collect_openshift.py) ---
    #
    # These run *inside* each cluster, deployed per-cluster by ArgoCD, and
    # answer a different question from the vendor collectors: not what
    # hardware exists, but whether a cluster is using it. They need no
    # endpoint and no credentials — a pod's own ServiceAccount token
    # reaches its own API server — so unlike every `ManagerType` above
    # there is nothing here to resolve through `EnvConnectionResolver`.

    # Which cluster this pod is in. Written onto every server the job
    # claims, and the scope it is allowed to release: a job frees only
    # servers already naming *its* cluster, never another's.
    openshift_cluster_name: str = ""

    # Which MCE this pod is, on a hub. Same role as the cluster name, for
    # the agents job.
    openshift_mce_name: str = ""

    # Nodes that do not count as fleet capacity, matched as substrings of
    # the node name, case-insensitively. Applied *after* the
    # `node-role.kubernetes.io/worker` label selector, not instead of it:
    # infra nodes usually carry the worker label too, so the label alone
    # keeps them, while a name list alone would misfire on a node called
    # `compute-infra-01`.
    openshift_exclude_name_parts: str = "infra,control-plane"

    openshift_request_timeout_seconds: float = 30.0

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _refuse_the_dev_cursor_secret_in_production(self) -> Settings:
        """
        Fail startup rather than sign a cursor with a secret anyone can read out of this file.

        `cursor_secret` was previously "only a code comment, not enforced
        at startup" (see CLAUDE.md's own correction of that claim) — an
        install that forgot to set `INVENTORY_CURSOR_SECRET` started up
        looking healthy and stayed on the committed default forever. A
        forged cursor is not a real information-disclosure risk yet (every
        endpoint is already open by design, convention 6), but it becomes
        one the moment authentication lands, and fixing it after that
        would mean rotating a secret every deployed cursor already
        depends on.

        Returns:
            Settings: `self`, unchanged, once the check passes.

        Raises:
            ValueError: If `environment` is `"production"` and
                `cursor_secret` is still the committed insecure default,
                or blank.
        """
        if self.environment != "production":
            return self
        if not self.cursor_secret.strip():
            # Reachable if a deployment wires `INVENTORY_CURSOR_SECRET` to
            # an empty `secretKeyRef` value (blank `stringData` still
            # counts as "set" to pydantic-settings, unlike leaving the env
            # var out entirely) — a different mistake from never setting
            # it at all, and one that must fail exactly the same way.
            raise ValueError(
                "INVENTORY_CURSOR_SECRET is blank with INVENTORY_ENVIRONMENT=production. "
                "Set INVENTORY_CURSOR_SECRET to a real, deployment-specific secret — "
                "see deploy/helm/server-scan's cursorSecret value."
            )
        if self.cursor_secret == _INSECURE_DEV_CURSOR_SECRET:
            raise ValueError(
                "INVENTORY_CURSOR_SECRET is still the committed dev default "
                f"({_INSECURE_DEV_CURSOR_SECRET!r}) with INVENTORY_ENVIRONMENT=production. "
                "Set INVENTORY_CURSOR_SECRET to a real, deployment-specific secret — "
                "see deploy/helm/server-scan's cursorSecret value."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """
    Return the cached `Settings` singleton, constructing it on first call.

    Tests can call `get_settings.cache_clear()` after
    `monkeypatch.setenv(...)` to pick up overrides.

    Returns:
        Settings: The process-wide settings instance.
    """
    return Settings()
