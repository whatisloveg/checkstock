import pytest
from sqlalchemy import select

from app import inventory
from app.inventory import Allocation
from app.models import Operation, OperationKind


async def test_transfer_is_in_transit_until_received(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]

    transfer = await inventory.send_transfer(session, sku, north, south, 4)
    state = await read_state()
    assert state["SKU-101"]["Север"] == (6, 0, 6)
    assert state["SKU-101"]["Юг"] == (6, 0, 6)
    assert state["SKU-101"]["в пути между ФФ"] == 4

    await inventory.receive_transfer(session, transfer.id)
    state = await read_state()
    assert state["SKU-101"]["Юг"] == (10, 0, 10)
    assert state["SKU-101"]["в пути между ФФ"] == 0


async def test_transfer_history_shows_where_from_and_where_to(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    transfer = await inventory.send_transfer(session, sku, north, south, 4)
    await inventory.receive_transfer(session, transfer.id)

    rows = await session.execute(
        select(
            Operation.kind, Operation.qty, Operation.from_warehouse_id, Operation.to_warehouse_id
        )
        .where(Operation.transfer_id == transfer.id)
        .order_by(Operation.id)
    )
    assert rows.all() == [
        (OperationKind.TRANSFER_SENT, 4, north, south),
        (OperationKind.TRANSFER_RECEIVED, 4, north, south),
    ]


async def test_repeated_transfer_receipt_counts_once(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    transfer = await inventory.send_transfer(session, sku, north, south, 4)
    transfer_id = transfer.id

    await inventory.receive_transfer(session, transfer_id)
    with pytest.raises(inventory.AlreadyDone, match="уже принято"):
        await inventory.receive_transfer(session, transfer_id)

    state = await read_state()
    assert state["SKU-101"]["Юг"] == (10, 0, 10)


async def test_reserved_goods_cannot_be_moved(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 8)])

    with pytest.raises(inventory.Shortage, match="доступно 2 из 4") as error:
        await inventory.send_transfer(session, sku, north, south, 4)

    assert error.value.split is None
    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 8, 2)


async def test_transfer_to_same_warehouse_is_rejected(session, data):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    with pytest.raises(inventory.InventoryError, match="тот же ФФ"):
        await inventory.send_transfer(session, sku, north, north, 1)


async def test_transfer_needs_positive_quantity(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]

    with pytest.raises(inventory.InventoryError, match="больше нуля"):
        await inventory.send_transfer(session, sku, north, south, 0)


async def test_unknown_transfer_is_reported(session, data):
    with pytest.raises(inventory.InventoryError, match="не найдено"):
        await inventory.receive_transfer(session, 999)
