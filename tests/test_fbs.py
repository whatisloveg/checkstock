import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import inventory
from app.inventory import Allocation
from app.models import FbsOrder, Operation, OperationKind
from tests.conftest import Seeded


async def reserve(session: AsyncSession, data: Seeded, qty: int = 3) -> FbsOrder:
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    [order] = await inventory.reserve_fbs(session, " WB-1 ", sku, [Allocation(north, qty)])
    return order


async def test_reserve_blocks_goods_but_keeps_them_on_warehouse(session, data, read_state):
    order = await reserve(session, data)

    assert order.order_number == "WB-1"
    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 3, 7)


async def test_shipped_goods_go_to_client(session, data, read_state):
    order = await reserve(session, data)

    await inventory.ship_fbs(session, order.id)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (7, 0, 7)
    assert state["SKU-101"]["FBS к клиенту"] == 3


async def test_cancel_returns_goods_to_available(session, data, read_state):
    order = await reserve(session, data)

    await inventory.cancel_fbs(session, order.id)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 0, 10)
    assert state["SKU-101"]["FBS к клиенту"] == 0


async def test_repeated_shipping_confirmation_ships_once(session, data, read_state):
    order_id = (await reserve(session, data)).id

    await inventory.ship_fbs(session, order_id)
    with pytest.raises(inventory.AlreadyDone, match="уже отправлен"):
        await inventory.ship_fbs(session, order_id)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (7, 0, 7)
    assert state["SKU-101"]["FBS к клиенту"] == 3


async def test_repeated_cancel_changes_nothing(session, data, read_state):
    order_id = (await reserve(session, data)).id

    await inventory.cancel_fbs(session, order_id)
    with pytest.raises(inventory.AlreadyDone, match="уже отменен"):
        await inventory.cancel_fbs(session, order_id)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 0, 10)


async def test_cancelled_order_cannot_be_shipped(session, data):
    order_id = (await reserve(session, data)).id
    await inventory.cancel_fbs(session, order_id)

    with pytest.raises(inventory.InventoryError, match="отменен"):
        await inventory.ship_fbs(session, order_id)


async def test_shipped_order_cannot_be_cancelled(session, data):
    order_id = (await reserve(session, data)).id
    await inventory.ship_fbs(session, order_id)

    with pytest.raises(inventory.InventoryError, match="отправлен"):
        await inventory.cancel_fbs(session, order_id)


async def test_reserve_needs_order_number(session, data):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    with pytest.raises(inventory.InventoryError, match="номер заказа"):
        await inventory.reserve_fbs(session, " ", sku, [Allocation(north, 1)])


async def test_unknown_order_is_reported(session, data):
    with pytest.raises(inventory.InventoryError, match="не найден"):
        await inventory.ship_fbs(session, 999)


async def test_fbs_history(session, data):
    order = await reserve(session, data)
    await inventory.ship_fbs(session, order.id)
    north = data.warehouses["Север"]

    rows = await session.execute(
        select(Operation.kind, Operation.qty, Operation.from_warehouse_id)
        .where(Operation.fbs_order_id == order.id)
        .order_by(Operation.id)
    )
    assert rows.all() == [
        (OperationKind.FBS_RESERVED, 3, north),
        (OperationKind.FBS_SHIPPED, 3, north),
    ]


async def test_same_order_cannot_be_reserved_twice(session, data, read_state):
    await reserve(session, data)

    with pytest.raises(inventory.InventoryError, match="Заказ WB-1 по SKU-101 уже зарезервирован"):
        await reserve(session, data)

    assert (await read_state())["SKU-101"]["Север"] == (10, 3, 7)


async def test_shipped_order_cannot_be_reserved_again(session, data):
    order_id = (await reserve(session, data)).id
    await inventory.ship_fbs(session, order_id)

    with pytest.raises(inventory.InventoryError, match="Заказ WB-1 по SKU-101 уже отправлен"):
        await reserve(session, data)


async def test_cancelled_order_can_be_reserved_again(session, data, read_state):
    order_id = (await reserve(session, data)).id
    await inventory.cancel_fbs(session, order_id)

    await reserve(session, data)

    assert (await read_state())["SKU-101"]["Север"] == (10, 3, 7)


async def test_one_order_can_hold_several_products(session, data, read_state):
    await reserve(session, data)
    north = data.warehouses["Север"]

    await inventory.reserve_fbs(session, "WB-1", data.products["SKU-102"], [Allocation(north, 1)])

    assert (await read_state())["SKU-102"]["Север"] == (5, 1, 4)


async def test_order_number_has_length_limit(session, data):
    north = data.warehouses["Север"]

    with pytest.raises(inventory.InventoryError, match="длиннее 64 символов"):
        await inventory.reserve_fbs(
            session, "W" * 65, data.products["SKU-101"], [Allocation(north, 1)]
        )
