import pytest

from app import inventory
from app.inventory import Allocation


async def test_fbo_goods_are_in_transit_until_accepted(session, data, read_state):
    sku, north = data.products["SKU-102"], data.warehouses["Север"]

    [shipment] = await inventory.send_fbo(session, sku, [Allocation(north, 5)])
    state = await read_state()
    assert state["SKU-102"]["Север"] == (0, 0, 0)
    assert state["SKU-102"]["FBO в пути"] == 5

    await inventory.accept_fbo(session, shipment.id)
    state = await read_state()
    assert state["SKU-102"]["FBO в пути"] == 0
    assert state["SKU-102"]["FBO принято"] == 5


async def test_repeated_fbo_acceptance_counts_once(session, data, read_state):
    sku, north = data.products["SKU-102"], data.warehouses["Север"]
    [shipment] = await inventory.send_fbo(session, sku, [Allocation(north, 5)])
    shipment_id = shipment.id

    await inventory.accept_fbo(session, shipment_id)
    with pytest.raises(inventory.AlreadyDone, match="уже принята"):
        await inventory.accept_fbo(session, shipment_id)

    state = await read_state()
    assert state["SKU-102"]["FBO принято"] == 5


async def test_fbo_cannot_take_reserved_goods(session, data, read_state):
    sku, north = data.products["SKU-102"], data.warehouses["Север"]
    await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 4)])

    with pytest.raises(inventory.Shortage, match="доступно 1 из 2"):
        await inventory.send_fbo(session, sku, [Allocation(north, 2)])

    state = await read_state()
    assert state["SKU-102"]["Север"] == (5, 4, 1)


async def test_unknown_shipment_is_reported(session, data):
    with pytest.raises(inventory.InventoryError, match="не найдена"):
        await inventory.accept_fbo(session, 999)
