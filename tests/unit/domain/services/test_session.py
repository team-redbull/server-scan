from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models.audit_event import Role
from app.domain.services.session import decode_session, encode_session

pytestmark = pytest.mark.unit

SECRET = "test-session-secret"


def test_round_trip() -> None:
    token = encode_session(username="jdoe", role=Role.ADMIN, secret=SECRET, ttl_seconds=3600)
    claims = decode_session(token, secret=SECRET)
    assert claims is not None
    assert claims.username == "jdoe"
    assert claims.role is Role.ADMIN


def test_tampered_signature_is_rejected() -> None:
    token = encode_session(username="jdoe", role=Role.ADMIN, secret=SECRET, ttl_seconds=3600)
    payload_b64, signature_b64 = token.split(".", 1)
    # The digest's *last* base64 char has 2 unchecked padding bits, so an
    # 'A'/'B' swap there can decode to identical bytes ~6% of the time
    # (flaky). The first char has no such ambiguity.
    tampered_char = "B" if signature_b64[0] != "B" else "C"
    tampered = f"{payload_b64}.{tampered_char}{signature_b64[1:]}"
    assert decode_session(tampered, secret=SECRET) is None


def test_signed_with_a_different_secret_is_rejected() -> None:
    token = encode_session(username="jdoe", role=Role.ADMIN, secret=SECRET, ttl_seconds=3600)
    assert decode_session(token, secret="wrong-secret") is None


def test_a_malformed_token_is_rejected_not_raised() -> None:
    assert decode_session("not-a-real-token", secret=SECRET) is None
    assert decode_session("", secret=SECRET) is None


def test_an_expired_token_is_rejected() -> None:
    token = encode_session(username="jdoe", role=Role.ADMIN, secret=SECRET, ttl_seconds=-1)
    assert decode_session(token, secret=SECRET) is None


def test_expires_at_is_close_to_now_plus_ttl() -> None:
    token = encode_session(username="jdoe", role=Role.VIEWER, secret=SECRET, ttl_seconds=3600)
    claims = decode_session(token, secret=SECRET)
    assert claims is not None
    expected = datetime.now(UTC) + timedelta(seconds=3600)
    assert abs((claims.expires_at - expected).total_seconds()) < 5
