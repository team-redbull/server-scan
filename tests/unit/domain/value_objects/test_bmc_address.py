from app.domain.value_objects.bmc_address import parse_bmc_address


def test_dell_idrac_virtualmedia_url() -> None:
    result = parse_bmc_address("idrac-virtualmedia://10.1.1.5/redfish/v1/Systems/System.Embedded.1")
    assert result is not None
    assert result.scheme == "idrac-virtualmedia"
    assert result.host == "10.1.1.5"
    assert result.host_is_ip is True
    assert result.port is None
    assert result.path == "/redfish/v1/Systems/System.Embedded.1"


def test_hpe_redfish_virtualmedia_url() -> None:
    result = parse_bmc_address("redfish-virtualmedia://10.1.1.6/redfish/v1/Systems/1")
    assert result is not None
    assert result.scheme == "redfish-virtualmedia"
    assert result.host == "10.1.1.6"
    assert result.path == "/redfish/v1/Systems/1"


def test_ipmi_with_explicit_port() -> None:
    result = parse_bmc_address("ipmi://10.1.1.7:623")
    assert result is not None
    assert result.scheme == "ipmi"
    assert result.host == "10.1.1.7"
    assert result.port == 623


def test_ipmi_without_port_defaults_to_623() -> None:
    result = parse_bmc_address("ipmi://10.1.1.7")
    assert result is not None
    assert result.port == 623


def test_non_ipmi_scheme_without_port_stays_none_not_guessed() -> None:
    # Redfish endpoints are frequently proxied on non-standard ports —
    # guessing 443 would be actively misleading.
    result = parse_bmc_address("https://10.1.1.8/redfish/v1")
    assert result is not None
    assert result.port is None


def test_bare_ip_with_no_scheme() -> None:
    result = parse_bmc_address("10.1.1.9")
    assert result is not None
    assert result.scheme is None
    assert result.host == "10.1.1.9"
    assert result.host_is_ip is True


def test_bare_hostname_with_no_scheme() -> None:
    result = parse_bmc_address("bmc-rack3-u12.dc1.example.internal")
    assert result is not None
    assert result.host == "bmc-rack3-u12.dc1.example.internal"
    assert result.host_is_ip is False


def test_ipv6_literal_in_brackets() -> None:
    result = parse_bmc_address("ipmi://[2001:db8::1]:623")
    assert result is not None
    assert result.host == "2001:db8::1"
    assert result.host_is_ip is True
    assert result.port == 623


def test_empty_and_none_input() -> None:
    assert parse_bmc_address(None) is None
    assert parse_bmc_address("") is None
    assert parse_bmc_address("   ") is None


def test_raw_string_is_preserved_verbatim() -> None:
    raw = "idrac-virtualmedia://10.1.1.5/redfish/v1/Systems/System.Embedded.1"
    result = parse_bmc_address(raw)
    assert result is not None
    assert result.raw == raw


def test_a_malformed_address_never_raises() -> None:
    """An unbalanced bracket gives no host; a bad port keeps the host and drops the port."""
    unbalanced = parse_bmc_address("[::1")
    bad_port = parse_bmc_address("https://bmc.example:99999")
    text_port = parse_bmc_address("host:abc")
    assert unbalanced is not None
    assert unbalanced.host is None
    assert bad_port is not None
    assert bad_port.host == "bmc.example"
    assert bad_port.port is None
    assert text_port is not None
    assert text_port.port is None
