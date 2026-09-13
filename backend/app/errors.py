"""Application error model.

Every raised `AppError` becomes an RFC 9457 ("Problem Details for HTTP
APIs") response via the handlers in `app.exception_handlers`:

    {
      "type": "/problems/revision-conflict",
      "title": "Revision Conflict",
      "status": 409,
      "detail": "The record was modified since you last read it.",
      "instance": "/api/v1/servers/srv_abc123",
      "code": "REVISION_CONFLICT",
      "request_id": "req_...",
      "details": {"current_revision": 5}
    }

RFC 9457 is the current IETF standard for HTTP API error bodies and is what
API-consuming tooling increasingly expects by default; `type`/`title`/
`status`/`detail`/`instance` are its fixed members, served with content-type
`application/problem+json`. `code`, `request_id`, and `details` are RFC 9457
"extension members" (explicitly permitted by the spec) that give this API's
own UI and any future automation a stable, machine-matchable error code on
top of the standard envelope — `code` never changes across a message
wording tweak, where `title`/`detail` might.

`type` is a relative reference (`/problems/<slug>`), not a dereferenceable
absolute URL — RFC 9457 explicitly allows this, and an air-gapped platform
has no public domain for it to resolve against anyway.

`ErrorCode` is an append-only registry: new codes are added at the end of
their section, and existing codes are never renumbered or removed once
shipped, since clients may match on them.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Stable, machine-matchable error identifiers returned to API clients."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    UNKNOWN_FILTER = "UNKNOWN_FILTER"
    UNKNOWN_SORT_FIELD = "UNKNOWN_SORT_FIELD"
    SEARCH_QUERY_TOO_SHORT = "SEARCH_QUERY_TOO_SHORT"
    SEARCH_QUERY_TOO_LONG = "SEARCH_QUERY_TOO_LONG"
    PAGE_SIZE_TOO_LARGE = "PAGE_SIZE_TOO_LARGE"
    CURSOR_INVALID = "CURSOR_INVALID"
    CURSOR_FILTER_MISMATCH = "CURSOR_FILTER_MISMATCH"
    REGEX_UNSAFE = "REGEX_UNSAFE"
    REGEX_INVALID = "REGEX_INVALID"
    RULE_SCOPE_INVALID = "RULE_SCOPE_INVALID"
    METRIC_OPERATOR_MISMATCH = "METRIC_OPERATOR_MISMATCH"
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    TEMPLATE_INVALID = "TEMPLATE_INVALID"
    CONDITION_INVALID = "CONDITION_INVALID"
    MANAGER_HAS_CHILDREN = "MANAGER_HAS_CHILDREN"
    INVALID_MANAGER_HIERARCHY = "INVALID_MANAGER_HIERARCHY"


def _slug(code: str) -> str:
    """Render an `ErrorCode` for the `type` member, e.g. REVISION_CONFLICT -> revision-conflict."""
    return code.lower().replace("_", "-")


def _title(code: str) -> str:
    """Render an `ErrorCode` for the `title` member, e.g. REVISION_CONFLICT -> Revision Conflict."""
    return code.replace("_", " ").title()


class AppError(Exception):
    """Base class for every error that becomes an RFC 9457 body.

    A raised `AppError` is an expected, handled condition; anything else
    reaches the client as an unhandled-exception 500.
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(self, detail: str, *, details: dict[str, Any] | None = None) -> None:
        """
        Build the error.

        Args:
            detail (str): Human-readable explanation, RFC 9457's `detail`.
            details (dict[str, Any] | None): Extra machine-readable
                context (RFC 9457's `details` extension member), or None.
        """
        super().__init__(detail)
        self.detail = detail
        self.details = details or {}

    @property
    def type(self) -> str:
        """RFC 9457's `type` member, derived from `code`."""
        return f"/problems/{_slug(self.code)}"

    @property
    def title(self) -> str:
        """RFC 9457's `title` member, derived from `code`."""
        return _title(self.code)


class ValidationAppError(AppError):
    """422: a request failed domain-level validation."""

    status_code = 422
    code = ErrorCode.VALIDATION_ERROR


class NotFoundError(AppError):
    """404: the requested resource does not exist."""

    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    """409: the request conflicts with the resource's current state."""

    status_code = 409
    code = ErrorCode.CONFLICT


class RevisionConflictError(ConflictError):
    """409: a write's `revision` no longer matches the stored document.

    Someone else wrote it first. See
    `app.domain.ports.repository.ServerRepository.upsert_with_revision_check`.
    """

    code = ErrorCode.REVISION_CONFLICT

    def __init__(self, detail: str, *, current_revision: int) -> None:
        """
        Build the error.

        Args:
            detail (str): Human-readable explanation.
            current_revision (int): The revision actually stored, so the
                client can decide whether to re-read and retry.
        """
        super().__init__(detail, details={"current_revision": current_revision})


class UnauthorizedError(AppError):
    """401: no valid credentials were presented."""

    status_code = 401
    code = ErrorCode.UNAUTHORIZED


class ForbiddenError(AppError):
    """403: the caller is identified but not permitted to do this."""

    status_code = 403
    code = ErrorCode.FORBIDDEN


class RateLimitedError(AppError):
    """429: the caller has exceeded a rate limit."""

    status_code = 429
    code = ErrorCode.RATE_LIMITED


class ServiceUnavailableError(AppError):
    """503: a dependency this request needed is down."""

    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE


class UnknownFilterError(AppError):
    """400: a `?filter` query param names a field `/servers` doesn't filter on."""

    status_code = 400
    code = ErrorCode.UNKNOWN_FILTER


class UnknownSortFieldError(AppError):
    """400: `?sort` names a field `/servers` doesn't sort on."""

    status_code = 400
    code = ErrorCode.UNKNOWN_SORT_FIELD


class SearchQueryTooShortError(AppError):
    """400: `?q` is shorter than the minimum search-query length."""

    status_code = 400
    code = ErrorCode.SEARCH_QUERY_TOO_SHORT


class SearchQueryTooLongError(AppError):
    """400: `?q` is longer than the maximum search-query length."""

    status_code = 400
    code = ErrorCode.SEARCH_QUERY_TOO_LONG


class PageSizeTooLargeError(AppError):
    """422, not 400 like its siblings: FastAPI's own convention for a query-parameter constraint."""

    status_code = 422
    code = ErrorCode.PAGE_SIZE_TOO_LARGE


class CursorInvalidError(AppError):
    """400: `?cursor` failed to decode or its HMAC signature doesn't verify."""

    status_code = 400
    code = ErrorCode.CURSOR_INVALID


class CursorFilterMismatchError(AppError):
    """400: `?cursor` was signed for different filters/sort than this request's."""

    status_code = 400
    code = ErrorCode.CURSOR_FILTER_MISMATCH


class RegexUnsafeAppError(AppError):
    """422: a classification rule's pattern failed the ReDoS safety check."""

    status_code = 422
    code = ErrorCode.REGEX_UNSAFE


class RegexInvalidAppError(AppError):
    """422: a classification rule's pattern doesn't compile at all."""

    status_code = 422
    code = ErrorCode.REGEX_INVALID


class RuleScopeInvalidError(AppError):
    """422: a classification rule's scope is malformed."""

    status_code = 422
    code = ErrorCode.RULE_SCOPE_INVALID


class MetricOperatorMismatchError(AppError):
    """422: a health policy condition's operator doesn't apply to its metric's type."""

    status_code = 422
    code = ErrorCode.METRIC_OPERATOR_MISMATCH


class UnknownMetricError(AppError):
    """422: a health policy condition names a metric not in the registry."""

    status_code = 422
    code = ErrorCode.UNKNOWN_METRIC


class TemplateInvalidError(AppError):
    """422: a health policy's message template references an undefined placeholder."""

    status_code = 422
    code = ErrorCode.TEMPLATE_INVALID


class ConditionInvalidError(AppError):
    """422: a health policy condition is structurally malformed."""

    status_code = 422
    code = ErrorCode.CONDITION_INVALID


class ManagerHasChildrenError(AppError):
    """409: a manager can't be removed while servers still reference it.

    Not raised by any current code path — managers are read-only projections
    of environment config (ADR-0012). Kept for `ErrorCode`'s append-only contract.
    """

    status_code = 409
    code = ErrorCode.MANAGER_HAS_CHILDREN


class InvalidManagerHierarchyError(AppError):
    """422: a manager's declared parent/child relationship is inconsistent.

    Not raised by any current code path, for the same reason as
    `ManagerHasChildrenError` above.
    """

    status_code = 422
    code = ErrorCode.INVALID_MANAGER_HIERARCHY
