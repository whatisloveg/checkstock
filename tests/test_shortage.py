import pytest
from sqlalchemy import func, select

from app import inventory
from app.inventory import Allocation
from app.models import FboShipment, FbsOrder


async def test_shortage_is_checked_on_chosen_warehouse_not_in_total(session, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    with pytest.raises(inventory.Shortage):
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 12)])

    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 0, 10)
    assert await session.scalar(select(func.count()).select_from(FbsOrder)) == 0


async def test_shortage_explains_where_goods_are(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]

    with pytest.raises(inventory.Shortage) as error:
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 12)])

    assert str(error.value) == "На ФФ Север доступно 10 из 12, не хватает 2. На других ФФ: Юг: 6"
    assert error.value.split == [Allocation(north, 10), Allocation(south, 2)]


async def test_split_reserve_is_created_in_one_go(session, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    with pytest.raises(inventory.Shortage) as error:
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 12)])

    split = error.value.split
    assert split is not None
    orders = await inventory.reserve_fbs(session, "A-1", sku, split)

    assert [(order.order_number, order.qty) for order in orders] == [("A-1", 10), ("A-1", 2)]
    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 10, 0)
    assert state["SKU-101"]["Юг"] == (6, 2, 4)


async def test_split_is_all_or_nothing(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]

    with pytest.raises(inventory.Shortage):
        await inventory.reserve_fbs(
            session, "A-1", sku, [Allocation(north, 10), Allocation(south, 7)]
        )

    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 0, 10)
    assert state["SKU-101"]["Юг"] == (6, 0, 6)


async def test_no_split_when_total_is_not_enough(session, data):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    with pytest.raises(inventory.Shortage, match="не хватает 10") as error:
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 20)])

    assert error.value.split is None


async def test_empty_warehouse_suggests_another_one(session, data):
    sku, north, south = data.products["SKU-103"], data.warehouses["Север"], data.warehouses["Юг"]

    with pytest.raises(inventory.Shortage, match="доступно 0 из 3") as error:
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 3)])

    assert error.value.split == [Allocation(south, 3)]


async def test_deleted_warehouse_is_mentioned_but_not_offered(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    await inventory.delete_warehouse(session, south)

    with pytest.raises(inventory.Shortage) as error:
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 12)])

    assert str(error.value) == (
        "На ФФ Север доступно 10 из 12, не хватает 2. На других ФФ товара нет. "
        "На удаленных ФФ, только перемещением: Юг: 6"
    )
    assert error.value.split is None


async def test_fbo_shortage_offers_split_too(session, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    with pytest.raises(inventory.Shortage) as error:
        await inventory.send_fbo(session, sku, [Allocation(north, 12)])

    split = error.value.split
    assert split is not None
    await inventory.send_fbo(session, sku, split)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (0, 0, 0)
    assert state["SKU-101"]["Юг"] == (4, 0, 4)
    assert state["SKU-101"]["FBO в пути"] == 12
    assert await session.scalar(select(func.count()).select_from(FboShipment)) == 2


async def test_same_warehouse_twice_is_rejected(session, data):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    with pytest.raises(inventory.InventoryError, match="дважды"):
        await inventory.reserve_fbs(
            session, "A-1", sku, [Allocation(north, 1), Allocation(north, 1)]
        )


async def test_empty_request_is_rejected(session, data):
    with pytest.raises(inventory.InventoryError, match="ФФ"):
        await inventory.send_fbo(session, data.products["SKU-101"], [])
