"""`KubeVirtClient` against an in-process facade (docs/adr/0058)."""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from tests.kubevirt_fixture import ROOT, SYSTEMS, KubeVirtFixture, Vm, kubevirt_tree

from app.infrastructure.providers.kubevirt.client import (
    KubeVirtClient,
    NotFoundError,
    build_verify,
    member_links,
    origin_of,
)
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishForbiddenError,
    RedfishProtocolError,
    RedfishTlsError,
    RedfishUnreachableError,
)

pytestmark = pytest.mark.unit

_VM = Vm(name="ocp-tlv-kv-01")
_PASSWORD = "secret"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    waits: list[str | None] = []

    async def instant(self: object, attempt: int, retry_after: str | None) -> None:
        waits.append(retry_after)

    monkeypatch.setattr(KubeVirtClient, "_backoff", instant)
    return waits


@pytest.fixture
def facade() -> Iterator[KubeVirtFixture]:
    with KubeVirtFixture(resources=kubevirt_tree(_VM)) as fixture:
        yield fixture


def _client(url: str, *, password: str = _PASSWORD, read_timeout: float = 5.0) -> KubeVirtClient:
    origin, _ = origin_of(url)
    return KubeVirtClient(
        origin=origin,
        username="scan",
        password=password,
        verify=False,
        connect_timeout=2.0,
        read_timeout=read_timeout,
    )


class TestOrigin:
    @pytest.mark.parametrize(
        ("url", "origin", "netloc"),
        [
            ("https://kv.apps.example", "https://kv.apps.example", "kv.apps.example"),
            ("https://kv.apps.example/", "https://kv.apps.example", "kv.apps.example"),
            ("https://kv.apps.example/redfish/v1", "https://kv.apps.example", "kv.apps.example"),
            ("https://kv:8443", "https://kv:8443", "kv:8443"),
            ("  http://10.0.0.1:8000  ", "http://10.0.0.1:8000", "10.0.0.1:8000"),
        ],
    )
    def test_keeps_only_scheme_host_and_port(self, url: str, origin: str, netloc: str) -> None:
        assert origin_of(url) == (origin, netloc)

    @pytest.mark.parametrize("url", ["", "kv.apps.example", "ftp://kv", "https://", "file:///x"])
    def test_refuses_anything_but_an_http_url(self, url: str) -> None:
        with pytest.raises(ValueError, match="INVENTORY_KUBEVIRT_URL"):
            origin_of(url)


class TestVerify:
    def test_the_opt_out_is_false(self) -> None:
        assert build_verify(verify_tls=False, ca_bundle="/nonexistent") is False

    def test_verifying_builds_a_tls12_context(self) -> None:
        context = build_verify(verify_tls=True, ca_bundle="")
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2


async def test_every_request_carries_basic_credentials(facade: KubeVirtFixture) -> None:
    async with _client(facade.url) as client:
        system = await client.get(_VM.path)

    assert system["Name"] == "ocp-tlv-kv-01"
    assert facade.requests == [(_VM.path, True)]


async def test_the_service_root_can_be_read_without_credentials(facade: KubeVirtFixture) -> None:
    async with _client(facade.url, password="wrong") as client:
        root = await client.get(ROOT, authenticated=False)

    assert root["Systems"] == {"@odata.id": SYSTEMS}
    assert facade.requests == [(ROOT, False)]
    assert facade.unauthorized_count() == 0


async def test_a_401_raises_after_exactly_one_request(facade: KubeVirtFixture) -> None:
    async with _client(facade.url, password="wrong") as client:
        with pytest.raises(RedfishAuthError, match="401"):
            await client.get(SYSTEMS)

    assert facade.paths() == [SYSTEMS]
    assert facade.unauthorized_count() == 1


async def test_a_403_is_forbidden_not_auth(facade: KubeVirtFixture) -> None:
    facade.faults[SYSTEMS] = 403
    async with _client(facade.url) as client:
        with pytest.raises(RedfishForbiddenError):
            await client.get(SYSTEMS)


async def test_a_404_is_its_own_error_with_the_status(facade: KubeVirtFixture) -> None:
    async with _client(facade.url) as client:
        with pytest.raises(NotFoundError) as caught:
            await client.get(f"{SYSTEMS}/vms.gone")

    assert caught.value.status_code == 404
    assert isinstance(caught.value, RedfishProtocolError)


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_a_server_error_is_not_retried(facade: KubeVirtFixture, status: int) -> None:
    facade.faults[SYSTEMS] = status
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError) as caught:
            await client.get(SYSTEMS)

    assert caught.value.status_code == status
    assert facade.paths() == [SYSTEMS]


@pytest.mark.parametrize("status", [429, 503])
async def test_429_and_503_are_retried_honouring_retry_after(
    facade: KubeVirtFixture, _no_backoff: list[str | None], status: int
) -> None:
    facade.faults[SYSTEMS] = status
    facade.fault_times[SYSTEMS] = 2
    facade.fault_headers[SYSTEMS] = {"Retry-After": "7"}
    async with _client(facade.url) as client:
        page = await client.get(SYSTEMS)

    assert member_links(page) == [_VM.path]
    assert facade.paths() == [SYSTEMS] * 3
    assert _no_backoff == ["7", "7"]


async def test_a_persistent_503_gives_up_after_three_attempts(facade: KubeVirtFixture) -> None:
    facade.faults[SYSTEMS] = 503
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError) as caught:
            await client.get(SYSTEMS)

    assert caught.value.status_code == 503
    assert facade.paths() == [SYSTEMS] * 3


async def test_a_redirect_is_refused_not_followed(facade: KubeVirtFixture) -> None:
    facade.redirects[SYSTEMS] = "https://elsewhere.example/redfish/v1/Systems"
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError, match="redirect"):
            await client.get(SYSTEMS)

    assert facade.paths() == [SYSTEMS]


async def test_an_oversized_body_is_refused(facade: KubeVirtFixture) -> None:
    facade.raw[SYSTEMS] = b'{"Members": [], "pad": "' + b"x" * (9 * 1024 * 1024) + b'"}'
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError, match="byte cap"):
            await client.get(SYSTEMS)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
async def test_a_non_object_body_is_a_protocol_error(facade: KubeVirtFixture, body: bytes) -> None:
    facade.raw[SYSTEMS] = body
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError):
            await client.get(SYSTEMS)


@pytest.mark.parametrize(
    "path", ["https://evil.example/redfish/v1/Systems", "/redfish/v1/../../etc", "/other"]
)
async def test_a_path_outside_the_tree_is_never_requested(
    facade: KubeVirtFixture, path: str
) -> None:
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError):
            await client.get(path)

    assert facade.requests == []


async def test_a_foreign_member_link_is_refused(facade: KubeVirtFixture) -> None:
    facade.resources[SYSTEMS] = {"Members": [{"@odata.id": "https://evil.example/x"}]}
    async with _client(facade.url) as client:
        with pytest.raises(RedfishProtocolError):
            await client.get_collection(SYSTEMS)

    assert facade.paths() == [SYSTEMS]


async def test_get_collection_follows_each_member(facade: KubeVirtFixture) -> None:
    async with _client(facade.url) as client:
        members = await client.get_collection(f"{_VM.path}/EthernetInterfaces")

    assert [m["Id"] for m in members] == ["default"]


def test_member_links_skips_malformed_entries() -> None:
    page = {"Members": [{"@odata.id": f"{SYSTEMS}/a"}, "junk", {}, {"@odata.id": ""}]}
    assert member_links(page) == [f"{SYSTEMS}/a"]
    assert member_links({"Members": None}) == []


async def test_a_closed_port_is_unreachable() -> None:
    with KubeVirtFixture() as fixture:
        url = fixture.url
    async with _client(url) as client:
        with pytest.raises(RedfishUnreachableError):
            await client.get(SYSTEMS)


async def test_a_stall_past_the_read_timeout_is_unreachable_and_not_retried(
    facade: KubeVirtFixture,
) -> None:
    facade.delays[SYSTEMS] = 0.5
    async with _client(facade.url, read_timeout=0.1) as client:
        with pytest.raises(RedfishUnreachableError, match="timed out"):
            await client.get(SYSTEMS)

    assert facade.paths() == [SYSTEMS]


def _self_signed(directory: Path) -> tuple[Path, Path]:
    """
    Write a self-signed certificate for 127.0.0.1 and its key.

    Args:
        directory (Path): Where to write them.

    Returns:
        tuple[Path, Path]: The certificate and key paths.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubevirt-redfish.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_facade(tmp_path: Path) -> Iterator[tuple[KubeVirtFixture, Path]]:
    cert, key = _self_signed(tmp_path)
    fixture = KubeVirtFixture(resources=kubevirt_tree(_VM)).start()
    assert fixture._server is not None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    fixture._server.socket = context.wrap_socket(fixture._server.socket, server_side=True)
    try:
        yield fixture, cert
    finally:
        fixture.stop()


def _tls_client(fixture: KubeVirtFixture, *, ca_bundle: str) -> KubeVirtClient:
    origin, _ = origin_of(fixture.url.replace("http://", "https://"))
    return KubeVirtClient(
        origin=origin,
        username="scan",
        password="secret",
        verify=build_verify(verify_tls=True, ca_bundle=ca_bundle),
        connect_timeout=2.0,
        read_timeout=5.0,
    )


async def test_an_untrusted_certificate_is_a_tls_error_and_no_credential_is_sent(
    tls_facade: tuple[KubeVirtFixture, Path],
) -> None:
    fixture, _ = tls_facade
    async with _tls_client(fixture, ca_bundle="") as client:
        with pytest.raises(RedfishTlsError, match="INVENTORY_KUBEVIRT_CA_BUNDLE"):
            await client.get(SYSTEMS)

    assert fixture.requests == []


async def test_the_ca_bundle_makes_the_same_certificate_trusted(
    tls_facade: tuple[KubeVirtFixture, Path],
) -> None:
    fixture, cert = tls_facade
    async with _tls_client(fixture, ca_bundle=str(cert)) as client:
        page = await client.get(SYSTEMS)

    assert member_links(page) == [_VM.path]
