"""`app.infrastructure.providers.intersight.signing`.

Intersight rejects a wrong signature, an expired key, a revoked key and a
drifted clock with the same bare 401, so a signing bug here is
indistinguishable in production from a credential problem. That makes
this the one part of the collector where an offline test has to carry the
whole weight.

Two properties are locked down: the *construction* — signing string,
header set, `Authorization` shape — which is exact and asserted
literally, and the *cryptography*, which is proved by verifying each
signature against its own public key rather than against a recorded
constant. No key material is committed; every key is generated here.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from app.infrastructure.providers.intersight.signing import (
    IntersightKeyError,
    IntersightSigner,
    load_private_key,
)

pytestmark = pytest.mark.unit

# Frozen so `Date` is a literal in the assertions below.
_NOW = 1756400000.0
_DATE = "Thu, 28 Aug 2025 16:53:20 GMT"
_KEY_ID = "61970b91aaaa/61970b91bbbb/626f24e5cccc"
_EMPTY_SHA256 = base64.b64encode(
    b"\xe3\xb0\xc4B\x98\xfc\x1c\x14\x9a\xfb\xf4\xc8\x99o\xb9$'\xaeA\xe4d\x9b\x93L\xa4\x95\x99\x1bxR\xb8U"
).decode()


def _rsa_pem() -> str:
    """
    A throwaway RSA (API key v2) private key.

    Returns:
        str: PEM text, `BEGIN RSA PRIVATE KEY`.
    """
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _ec_pem() -> str:
    """
    A throwaway EC (API key v3) private key.

    Returns:
        str: PEM text, `BEGIN EC PRIVATE KEY`.
    """
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _sign(pem: str, *, query: str = "%24top=1000") -> dict[str, str]:
    """
    Sign one representative request.

    Args:
        pem (str): The private key.
        query (str): The already-encoded query string.

    Returns:
        dict[str, str]: The signed headers.
    """
    return (
        IntersightSigner(key_id=_KEY_ID, private_key_pem=pem)
        .sign(
            method="GET",
            path="/api/v1/compute/PhysicalSummaries",
            query=query,
            host="intersight.com",
            now=_NOW,
        )
        .headers
    )


def _signing_string(headers: dict[str, str], *, query: str = "%24top=1000") -> bytes:
    """
    Rebuild the exact bytes the signature must cover, independently of the
    implementation so a construction change fails here instead of agreeing
    with itself.

    Args:
        headers (dict[str, str]): The signed headers.
        query (str): The query string that was signed.

    Returns:
        bytes: The signing string.
    """
    return "\n".join(
        [
            f"(request-target): get /api/v1/compute/PhysicalSummaries?{query}",
            "host: intersight.com",
            f"date: {headers['Date']}",
            f"digest: {headers['Digest']}",
        ]
    ).encode()


def test_the_signed_header_set_is_cisco_s_own() -> None:
    """`(request-target)`, `host`, `date`, `digest` — in that order.

    Not the draft standard's default of `(created)` alone, which the
    official SDK uses and Intersight rejects.
    """
    headers = _sign(_rsa_pem())
    assert 'headers="(request-target) host date digest"' in headers["Authorization"]
    assert "created=" not in headers["Authorization"]
    assert "expires=" not in headers["Authorization"]


def test_the_authorization_header_has_the_documented_shape() -> None:
    """Key id and scheme are quoted, and the scheme is `hs2019`."""
    headers = _sign(_rsa_pem())
    assert headers["Authorization"].startswith(
        f'Signature keyId="{_KEY_ID}",algorithm="hs2019",headers='
    )
    assert re.search(r',signature="[A-Za-z0-9+/=]+"$', headers["Authorization"])


def test_date_and_digest_come_from_the_clock_and_an_empty_body() -> None:
    """Every request this collector makes is a bodiless GET."""
    headers = _sign(_rsa_pem())
    assert headers["Date"] == _DATE
    assert headers["Digest"] == f"SHA-256={_EMPTY_SHA256}"
    assert headers["Host"] == "intersight.com"


@pytest.mark.parametrize("pem_factory", [_rsa_pem, _ec_pem], ids=["rsa-v2", "ec-v3"])
def test_the_signature_verifies_against_its_own_public_key(
    pem_factory: Callable[[], str],
) -> None:
    """The cryptographic half, for both API key generations. An RSA key must
    sign PKCS1v15 — the library default is PSS, which Intersight rejects
    for a v2 key (docs/cisco-collectors.md).
    """
    pem = pem_factory()
    headers = _sign(pem)
    public = load_private_key(pem).public_key()
    match = re.search(r'signature="([^"]+)"', headers["Authorization"])
    assert match is not None
    signature = base64.b64decode(match.group(1))
    message = _signing_string(headers)

    if isinstance(public, rsa.RSAPublicKey):
        public.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    else:
        public.verify(signature, message, ec.ECDSA(hashes.SHA256()))


def test_the_query_string_is_covered_by_the_signature() -> None:
    """A query that is signed differently from how it is sent is a 401.

    Two requests differing only in their query must not produce the same
    signature — which is what proves `(request-target)` really carries it.
    """
    pem = _rsa_pem()
    assert (
        _sign(pem, query="%24top=1000")["Authorization"]
        != _sign(pem, query="%24top=1")["Authorization"]
    )


def test_an_rsa_signature_is_reproducible_for_a_fixed_key_and_clock() -> None:
    """PKCS1v15 is deterministic, so the same inputs sign identically.

    Worth pinning: it is what makes a signing regression show up as a
    changed value rather than as intermittent authentication failures.
    """
    pem = _rsa_pem()
    assert _sign(pem)["Authorization"] == _sign(pem)["Authorization"]


def test_a_password_instead_of_a_pem_says_so() -> None:
    """The single most likely misconfiguration, given the field is called
    `INVENTORY_INTERSIGHT_API_KEY_PEM`.
    """
    with pytest.raises(IntersightKeyError, match="not a PEM private key"):
        IntersightSigner(key_id=_KEY_ID, private_key_pem="hunter2")


def test_a_missing_key_id_says_which_variable_to_set() -> None:
    """An operator who set the PEM but not the id gets told which."""
    with pytest.raises(IntersightKeyError, match="INVENTORY_INTERSIGHT_API_KEY_ID"):
        IntersightSigner(key_id="   ", private_key_pem=_rsa_pem())


def test_a_truncated_pem_is_rejected_before_any_request() -> None:
    """Detected locally, so it never presents as a credential problem."""
    pem = _rsa_pem()
    with pytest.raises(IntersightKeyError, match="could not be parsed"):
        IntersightSigner(key_id=_KEY_ID, private_key_pem=pem[: len(pem) // 2] + "-----END\n")


def test_an_encrypted_key_is_rejected_with_the_reason() -> None:
    """Supported by the vendor SDK, deliberately not here — so the
    message has to say that rather than look like a parse failure.
    """
    encrypted = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        )
        .decode()
    )
    with pytest.raises(IntersightKeyError, match="passphrase-protected"):
        IntersightSigner(key_id=_KEY_ID, private_key_pem=encrypted)
