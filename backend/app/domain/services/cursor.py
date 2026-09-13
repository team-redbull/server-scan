"""Opaque, signed keyset-pagination cursors.

A cursor encodes the `(sort_field_value, _id)` position keyset pagination
resumes from (see `app.infrastructure.mongodb.server_repository`), plus a
digest binding it to the exact filter/sort/direction/page_size combination
it was issued for. Two things this buys us:

1. A client can never forge or hand-edit a cursor to walk arbitrary
   document positions — the payload is HMAC-SHA256 signed with a
   server-only secret (`Settings.cursor_secret`), so tampering is detected
   (`CursorInvalidError`) rather than silently accepted.
2. A client that changes a filter or sort mid-pagination and replays an
   old cursor gets a clear `CursorFilterMismatchError` instead of a
   silently wrong page (some rows skipped, others repeated) — the binding
   hash is recomputed from the *current* request's filters/sort/direction/
   page_size and compared against the one embedded at encode time.

The cursor is base64url text with no padding, safe to place directly in a
query string: `"<payload_b64>.<signature_b64>"`. Nothing about its
structure is meant to be inspected by a client — treat it as opaque.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from app.errors import CursorFilterMismatchError, CursorInvalidError
from app.utils.digest import stable_hash

# A null is tagged, not encoded as "": the two sort differently (ADR-0026).
_TYPE_STR = "str"
_TYPE_DATETIME = "datetime"
_TYPE_NULL = "null"


@dataclass(frozen=True, slots=True)
class CursorPosition:
    """The decoded `(sort_field_value, _id)` position to resume listing from."""

    sort_value: str | datetime | None
    id_value: str


def _binding_hash(*, filters: dict[str, object], sort: str, sort_desc: bool, page_size: int) -> str:
    return stable_hash(
        {"filters": filters, "sort": sort, "sort_desc": sort_desc, "page_size": page_size}
    )


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _serialize_value(value: str | datetime | None) -> tuple[str, str]:
    if value is None:
        return "", _TYPE_NULL
    if isinstance(value, datetime):
        return value.isoformat(), _TYPE_DATETIME
    return value, _TYPE_STR


def _deserialize_value(value: str, value_type: str) -> str | datetime | None:
    if value_type == _TYPE_NULL:
        return None
    if value_type == _TYPE_DATETIME:
        return datetime.fromisoformat(value)
    return value


def encode_cursor(
    *,
    sort_value: str | datetime | None,
    id_value: str,
    filters: dict[str, object],
    sort: str,
    sort_desc: bool,
    page_size: int,
    secret: str,
) -> str:
    """Build an opaque cursor pointing just past `(sort_value, id_value)`."""
    value_repr, value_type = _serialize_value(sort_value)
    payload = {
        "v": value_repr,
        "vt": value_type,
        "id": id_value,
        "fh": _binding_hash(filters=filters, sort=sort, sort_desc=sort_desc, page_size=page_size),
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{_b64encode(signature)}"


def decode_cursor(
    cursor: str,
    *,
    filters: dict[str, object],
    sort: str,
    sort_desc: bool,
    page_size: int,
    secret: str,
) -> CursorPosition:
    """
    Verify and decode a cursor produced by `encode_cursor`.

    Args:
        cursor (str): The opaque cursor string from the request.
        filters (dict[str, object]): The current request's whitelisted filters.
        sort (str): The current sort field.
        sort_desc (bool): The current sort direction.
        page_size (int): The current page size.
        secret (str): The HMAC key the cursor was signed with.

    Returns:
        CursorPosition: The position to resume from.

    Raises:
        CursorInvalidError: Bad base64, bad signature, or a malformed payload.
        CursorFilterMismatchError: A valid signature whose filter/sort/
            direction/page_size binding no longer matches this request.
    """
    try:
        payload_b64, signature_b64 = cursor.split(".", 1)
        expected_signature = hmac.new(
            secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_signature = _b64decode(signature_b64)
    except (ValueError, TypeError):
        raise CursorInvalidError("The pagination cursor is malformed.") from None

    if not hmac.compare_digest(expected_signature, actual_signature):
        raise CursorInvalidError("The pagination cursor signature is invalid.")

    try:
        payload = json.loads(_b64decode(payload_b64))
        sort_value = _deserialize_value(str(payload["v"]), str(payload["vt"]))
        id_value = str(payload["id"])
        filter_hash = str(payload["fh"])
    except (ValueError, TypeError, KeyError) as exc:
        raise CursorInvalidError("The pagination cursor payload is malformed.") from exc

    expected_hash = _binding_hash(
        filters=filters, sort=sort, sort_desc=sort_desc, page_size=page_size
    )
    if not hmac.compare_digest(filter_hash, expected_hash):
        raise CursorFilterMismatchError(
            "The filters, sort, or page size changed since this cursor was issued."
        )

    return CursorPosition(sort_value=sort_value, id_value=id_value)
