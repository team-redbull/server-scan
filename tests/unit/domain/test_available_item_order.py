"""`/servers/available` lists a server's interfaces in natural order of their own name."""

from app.api.v1.schemas import AvailableServerItem
from app.domain.enums import Vendor
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Identity, Server
from app.domain.value_objects.nic_names import nic_name_catalog
from app.utils.timeutil import utcnow


def _item(vendor: Vendor, names: list[str]) -> AvailableServerItem:
    """
    Project a server with the given interface names through `from_server`.

    Args:
        vendor (Vendor): The server's vendor.
        names (list[str]): Interface names, in stored order.

    Returns:
        AvailableServerItem: The projected item.
    """
    now = utcnow()
    server = Server(
        _id="srv_x",
        name="x",
        identity=Identity(vendor=vendor),
        network=NetworkInfo(interfaces=[NetworkInterface(name=n) for n in names]),
        created_at=now,
        updated_at=now,
    )
    return AvailableServerItem.from_server(
        server, nic_names=nic_name_catalog(""), live_recheck_performed=False
    )


def test_interfaces_come_out_in_natural_name_order() -> None:
    item = _item(Vendor.DELL, ["eno10", "eno2", "eno1"])
    assert [i.name for i in item.interfaces] == ["eno1", "eno2", "eno10"]


def test_sort_uses_the_name_never_the_cisco_os_name() -> None:
    item = _item(Vendor.CISCO, ["eth10", "eth2", "eth1"])
    assert [i.name for i in item.interfaces] == ["eth1", "eth2", "eth10"]
    assert [i.os_name for i in item.interfaces] == ["eno7", "eno6", "eno5"]
