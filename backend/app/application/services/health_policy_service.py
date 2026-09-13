"""The one integration seam for health-policy evaluation.

`HealthPolicyService` ties together policy loading, fact extraction, and
the domain evaluation engine (`app.domain.services.health.evaluate.
evaluate_health`) so callers never have to remember to do those three
steps in the right order themselves.

Two ways to reach it, mirroring `ClassificationService`'s own
`classify_server` / `load_ruleset`+`classify_with_ruleset` split, and for
the same reason: `evaluate_server` loads every stored policy fresh on
every call (right for a single, standalone evaluation — the
`POST /servers/{id}/health/recalculate` route); `load_policies` +
`evaluate_with_policies` split that load out for a caller evaluating many
servers in one run — the ingestion pipeline
(`app.application.services.ingest`), which used to call `evaluate_server`
per server and, per P1 in `docs/notes/2026-09-audit.md`, was issuing
~10,000 uncached collection reads on a 10,000-server run for an answer
that cannot change during that run.

`validate_policy_write` is a free function, not a method, for the same
reason `classification_service.validate_rule_write` is: its only caller
today is `app.application.services.bootstrap`, validating the shipped
system defaults at startup, since the create/update API routes it was
written for are gone (health policies are read-only now; `27b20a8`,
`f9ab059`).

A `preview()` method (whether a DRAFT policy would be the effective,
firing evaluation for existing servers) and `validate_system_field_lock`
(guarding a `system=True` policy's partial-update payload) both used to
live here too, for the health-policy editor UI. Deleted along with that
UI and its `/preview` endpoint: zero callers were left once the editors
went (see `f9ab059`'s commit message).
"""

from __future__ import annotations

from app.domain.models.health_policy import HealthPolicy
from app.domain.models.server import Server
from app.domain.services.health.conditions import ConditionValidationError, validate_condition
from app.domain.services.health.evaluate import HealthState, evaluate_health
from app.domain.services.health.facts import extract_facts
from app.domain.services.health.metrics import MetricRegistry
from app.domain.services.health.template import TemplateValidationError, validate_template
from app.errors import (
    ConditionInvalidError,
    MetricOperatorMismatchError,
    TemplateInvalidError,
    UnknownMetricError,
    ValidationAppError,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository

# Source -> which single scope field that source requires to be set (and,
# for GLOBAL_CUSTOM, requires *not* to be set). Mirrors the classification
# rule scope/source coherence rule described in the task brief; kept here
# rather than in `app.domain.models.health_policy` because it's a
# write-time business rule, not a structural invariant of the model
# itself (the model's own validators only enforce priority-band and mode
# validity — see that module's docstring).
_SCOPE_REQUIREMENTS: dict[str, str] = {
    "SITE_CUSTOM": "site_id",
    "MANAGER_CUSTOM": "manager_types",
    "VENDOR_CUSTOM": "vendor",
}
_KNOWN_SOURCES = frozenset(
    {"SITE_CUSTOM", "MANAGER_CUSTOM", "VENDOR_CUSTOM", "GLOBAL_CUSTOM", "SYSTEM_DEFAULT"}
)


def _validate_scope_source_coherence(policy: HealthPolicy) -> None:
    source = policy.source
    scope = policy.scope

    if source not in _KNOWN_SOURCES:
        raise ValidationAppError(f"Unknown source {source!r}.", details={"source": source})

    required_field = _SCOPE_REQUIREMENTS.get(source)
    if required_field is not None and not getattr(scope, required_field):
        raise ValidationAppError(
            f"{source} policies must set scope.{required_field}.",
            details={"source": source, "missing_field": required_field},
        )

    if source == "GLOBAL_CUSTOM" and (
        scope.site_id is not None or scope.manager_types or scope.vendor is not None
    ):
        raise ValidationAppError(
            "GLOBAL_CUSTOM policies must not set any scope field.",
            details={"source": source, "scope": scope.model_dump()},
        )


def validate_policy_write(policy: HealthPolicy, *, registry: MetricRegistry) -> None:
    """
    Validate what `HealthPolicy`'s own model validators don't already enforce.

    Checks condition safety against the metric registry, template safety
    against the declared evidence keys, and source/scope coherence —
    beyond the model's own priority-band and mode-validity checks (see
    `HealthPolicy`'s docstring). Called by
    `app.application.services.bootstrap` before seeding or re-syncing a
    shipped system default.

    `ConditionValidationError` messages are pattern-matched to decide
    between the three health-policy error codes that already exist for
    condition problems (`UNKNOWN_METRIC`, `METRIC_OPERATOR_MISMATCH`,
    generic `CONDITION_INVALID`) — the domain layer raises one exception
    type for all of these, so message content is the only signal
    available here without changing that (fixed, read-only) contract.

    Args:
        policy (HealthPolicy): The fully-merged policy that would be written.
        registry (MetricRegistry): The known metrics, for condition validation.

    Raises:
        UnknownMetricError: The condition references a metric that doesn't exist.
        MetricOperatorMismatchError: An operator doesn't apply to its metric's type.
        ConditionInvalidError: Any other condition problem.
        TemplateInvalidError: `message_template` references an undeclared evidence key.
        ValidationAppError: The source/scope combination is invalid.
    """
    try:
        validate_condition(policy.condition, registry)
    except ConditionValidationError as exc:
        message = str(exc)
        if message.startswith("unknown metric"):
            raise UnknownMetricError(message) from exc
        if "is not valid for metric type" in message:
            raise MetricOperatorMismatchError(message) from exc
        raise ConditionInvalidError(message) from exc

    try:
        validate_template(policy.message_template, {e.key for e in policy.evidence})
    except TemplateValidationError as exc:
        raise TemplateInvalidError(str(exc)) from exc

    _validate_scope_source_coherence(policy)


class HealthPolicyService:
    """
    The only place `extract_facts` + policy loading + `evaluate_health` are wired together.

    Callers never have to assemble those three steps themselves.
    """

    def __init__(
        self,
        *,
        policy_repo: MongoHealthPolicyRepository,
        registry: MetricRegistry,
    ) -> None:
        """
        Initialize the service with its policy repository and metric registry.

        Args:
            policy_repo (MongoHealthPolicyRepository): The health policies collection.
            registry (MetricRegistry): The known metrics, for fact/condition evaluation.
        """
        self._policy_repo = policy_repo
        self._registry = registry

    async def evaluate_server(self, server: Server) -> HealthState:
        """
        Load every stored policy fresh and evaluate against this server's facts.

        The right choice for a single, standalone evaluation against
        whatever is current right now (the
        `POST /servers/{id}/health/recalculate` route). A caller
        evaluating many servers in one run should call `load_policies`
        once instead and `evaluate_with_policies` per server; see
        `load_policies`'s docstring for why.

        Args:
            server (Server): The server to evaluate.

        Returns:
            HealthState: The resolved overall severity and every firing/
                suppressed evaluation.
        """
        policies = await self.load_policies()
        return self.evaluate_with_policies(server, policies)

    async def load_policies(self) -> list[HealthPolicy]:
        """
        Every stored policy, for a caller evaluating many servers in one run.

        Scope filtering happens inside `evaluate_health` itself, not here.
        The ingestion pipeline calls this once and reuses the result,
        rather than a fresh `evaluate_server` call per server — before
        this existed, a 10,000-server run issued ~10,000 uncached
        collection reads for an answer that cannot change during the run.

        Returns:
            list[HealthPolicy]: The policies to pass into
                `evaluate_with_policies` for the rest of the run.
        """
        return await self._policy_repo.list_all()

    def evaluate_with_policies(self, server: Server, policies: list[HealthPolicy]) -> HealthState:
        """
        Evaluate against an already-loaded policy set (`load_policies`).

        The ingest-loop counterpart to `evaluate_server`, which loads its
        own policy set on every call. `Server.source_provider` is the
        collector's `ManagerType` value and is what manager-scoped
        policies match against.

        Args:
            server (Server): The server to evaluate.
            policies (list[HealthPolicy]): The policies loaded by
                `load_policies` for this run.

        Returns:
            HealthState: The resolved overall severity and every firing/
                suppressed evaluation.
        """
        facts = extract_facts(server)
        return evaluate_health(
            facts,
            policies,
            self._registry,
            vendor=server.identity.vendor.value,
            manager_type=server.source_provider,
            site_id=server.site_id,
        )
