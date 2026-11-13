import pytest
from sqlalchemy import select

from app import inventory
from app.inventory import Allocation
from app.models import Warehouse


async def test_rename_keeps_stock(session, data, read_state):
    await inventory.rename_warehouse(session, data.warehouses["Север"], " Север-2 ")

    state = await read_state()
    assert state["SKU-101"]["Север-2"] == (10, 0, 10)
    assert "Север" not in state["SKU-101"]


async def test_rename_to_taken_name_is_rejected(session, data):
    with pytest.raises(inventory.InventoryError, match="уже есть"):
        await inventory.rename_warehouse(session, data.warehouses["Север"], "юг")


async def test_rename_needs_name(session, data):
    with pytest.raises(inventory.InventoryError, match="название"):
        await inventory.rename_warehouse(session, data.warehouses["Север"], "  ")


async def test_deleted_warehouse_cannot_be_renamed(session, data):
    await inventory.delete_warehouse(session, data.warehouses["Юг"])

    with pytest.raises(inventory.InventoryError, match="удален"):
        await inventory.rename_warehouse(session, data.warehouses["Юг"], "Восток")


async def test_deleted_warehouse_keeps_stock_on_screen(session, data, read_state):
    await inventory.delete_warehouse(session, data.warehouses["Юг"])

    state = await read_state()
    assert state["SKU-101"]["Юг"] == (6, 0, 6)
    assert state["SKU-103"]["Юг"] == (4, 0, 4)
    south = await session.get_one(Warehouse, data.warehouses["Юг"])
    assert south.deleted_at is not None


async def test_deleted_empty_warehouse_disappears_from_screen(session, data, read_state):
    west = await inventory.create_warehouse(session, "Запад")
    await inventory.delete_warehouse(session, west.id)

    state = await read_state()
    assert "Запад" not in state["SKU-101"]


async def test_deleting_twice_changes_nothing(session, data):
    await inventory.delete_warehouse(session, data.warehouses["Юг"])

    with pytest.raises(inventory.AlreadyDone):
        await inventory.delete_warehouse(session, data.warehouses["Юг"])


async def test_unknown_warehouse_is_reported(session, data):
    with pytest.raises(inventory.InventoryError, match="не найден"):
        await inventory.delete_warehouse(session, 999)


async def test_goods_can_be_moved_out_of_deleted_warehouse(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    await inventory.delete_warehouse(session, south)

    transfer = await inventory.send_transfer(session, sku, south, north, 6)
    await inventory.receive_transfer(session, transfer.id)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (16, 0, 16)
    assert state["SKU-101"]["Юг"] == (0, 0, 0)


async def test_deleted_warehouse_takes_no_new_goods_or_reserves(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    await inventory.delete_warehouse(session, south)

    with pytest.raises(inventory.InventoryError, match="удален"):
        await inventory.send_transfer(session, sku, north, south, 1)
    with pytest.raises(inventory.InventoryError, match="удален"):
        await inventory.reserve_fbs(session, "A-1", sku, [Allocation(south, 1)])
    with pytest.raises(inventory.InventoryError, match="удален"):
        await inventory.send_fbo(session, sku, [Allocation(south, 1)])


async def test_started_operations_finish_after_deletion(session, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    [order] = await inventory.reserve_fbs(session, "A-1", sku, [Allocation(south, 2)])
    transfer = await inventory.send_transfer(session, sku, north, south, 3)
    await inventory.delete_warehouse(session, south)

    await inventory.ship_fbs(session, order.id)
    await inventory.receive_transfer(session, transfer.id)

    state = await read_state()
    assert state["SKU-101"]["Юг"] == (7, 0, 7)
    assert state["SKU-101"]["FBS к клиенту"] == 2


async def test_deleted_warehouse_stays_in_history(session, data):
    await inventory.delete_warehouse(session, data.warehouses["Юг"])

    names = await session.scalars(select(Warehouse.name).order_by(Warehouse.id))
    assert list(names) == ["Север", "Юг"]
