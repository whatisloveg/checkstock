import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, update

from app import inventory
from app.inventory import Allocation
from app.models import Warehouse


async def test_parallel_reserves_never_oversell(sessions, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    async def reserve(number: int) -> bool:
        async with sessions() as session:
            try:
                await inventory.reserve_fbs(session, f"A-{number}", sku, [Allocation(north, 3)])
            except inventory.Shortage:
                return False
            return True

    results = await asyncio.gather(*(reserve(number) for number in range(6)))

    assert results.count(True) == 3
    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 9, 1)


async def test_parallel_transfers_and_fbo_never_oversell(sessions, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]

    async def move(fbo: bool) -> bool:
        async with sessions() as session:
            try:
                if fbo:
                    await inventory.send_fbo(session, sku, [Allocation(north, 4)])
                else:
                    await inventory.send_transfer(session, sku, north, south, 4)
            except inventory.Shortage:
                return False
            return True

    results = await asyncio.gather(*(move(fbo=i % 2 == 0) for i in range(6)))

    assert results.count(True) == 2
    state = await read_state()
    assert state["SKU-101"]["Север"] == (2, 0, 2)


async def test_parallel_shipping_confirmations_ship_once(session, sessions, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    [order] = await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 3)])

    async def ship() -> bool:
        async with sessions() as session:
            try:
                await inventory.ship_fbs(session, order.id)
            except inventory.AlreadyDone:
                return False
            return True

    results = await asyncio.gather(*(ship() for _ in range(5)))

    assert results.count(True) == 1
    state = await read_state()
    assert state["SKU-101"]["Север"] == (7, 0, 7)
    assert state["SKU-101"]["FBS к клиенту"] == 3


async def test_parallel_transfer_receipts_count_once(session, sessions, data, read_state):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    transfer = await inventory.send_transfer(session, sku, north, south, 4)

    async def accept() -> bool:
        async with sessions() as session:
            try:
                await inventory.receive_transfer(session, transfer.id)
            except inventory.AlreadyDone:
                return False
            return True

    results = await asyncio.gather(*(accept() for _ in range(5)))

    assert results.count(True) == 1
    state = await read_state()
    assert state["SKU-101"]["Юг"] == (10, 0, 10)


async def test_double_submitted_form_is_applied_once(sessions, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    request_id = uuid4()

    async def submit() -> bool:
        async with sessions() as session:
            try:
                await inventory.receive(session, sku, north, 5, request_id=request_id)
            except inventory.AlreadyDone:
                return False
            return True

    results = await asyncio.gather(*(submit() for _ in range(4)))

    assert results.count(True) == 1
    state = await read_state()
    assert state["SKU-101"]["Север"] == (15, 0, 15)


async def test_ship_and_cancel_at_once_do_only_one(session, sessions, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    [order] = await inventory.reserve_fbs(session, "A-1", sku, [Allocation(north, 3)])

    async def act(ship: bool) -> str:
        async with sessions() as session:
            try:
                if ship:
                    await inventory.ship_fbs(session, order.id)
                else:
                    await inventory.cancel_fbs(session, order.id)
            except inventory.InventoryError:
                return "отказ"
            return "отправлен" if ship else "отменен"

    results = await asyncio.gather(act(ship=True), act(ship=False))

    assert results.count("отказ") == 1
    state = await read_state()
    assert state["SKU-101"]["Север"] in [(7, 0, 7), (10, 0, 10)]


async def test_same_order_from_two_tabs_is_reserved_once(sessions, data, read_state):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]

    async def submit() -> bool:
        async with sessions() as session:
            try:
                await inventory.reserve_fbs(session, "WB-1", sku, [Allocation(north, 3)])
            except inventory.InventoryError:
                return False
            return True

    results = await asyncio.gather(*(submit() for _ in range(4)))

    assert results.count(True) == 1
    assert (await read_state())["SKU-101"]["Север"] == (10, 3, 7)


async def test_operation_waits_while_warehouse_is_being_deleted(sessions, data):
    sku, south = data.products["SKU-101"], data.warehouses["Юг"]
    async with sessions() as deleting, sessions() as receiving:
        async with deleting.begin():
            await deleting.execute(
                update(Warehouse).where(Warehouse.id == south).values(deleted_at=func.now())
            )
            receipt = asyncio.create_task(inventory.receive(receiving, sku, south, 1))
            await asyncio.sleep(0.5)
            assert not receipt.done()

        with pytest.raises(inventory.InventoryError, match="Юг удален"):
            await receipt
