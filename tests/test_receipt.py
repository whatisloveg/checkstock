from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import inventory
from app.models import Operation, OperationKind, Product, Stock


async def make_product(session: AsyncSession, sku: str = "SKU-101") -> Product:
    product = Product(sku=sku, name="Органайзер")
    session.add(product)
    await session.commit()
    return product


async def stock_of(
    sessions: async_sessionmaker[AsyncSession], product_id: int, warehouse_id: int
) -> Stock:
    async with sessions() as session:
        return await session.get_one(Stock, (product_id, warehouse_id))


async def test_receipt_adds_physical_and_available_stock(session, sessions):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")

    await inventory.receive(session, product.id, north.id, 10)
    await inventory.receive(session, product.id, north.id, 3)

    stock = await stock_of(sessions, product.id, north.id)
    assert (stock.on_hand, stock.reserved, stock.available) == (13, 0, 13)


async def test_receipt_is_written_to_history(session):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")

    await inventory.receive(session, product.id, north.id, 10)

    operation = (await session.scalars(select(Operation))).one()
    assert operation.kind == OperationKind.RECEIPT
    assert (operation.product_id, operation.qty) == (product.id, 10)
    assert (operation.from_warehouse_id, operation.to_warehouse_id) == (None, north.id)


@pytest.mark.parametrize("qty", [0, -5])
async def test_receipt_needs_positive_quantity(session, qty):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")

    with pytest.raises(inventory.InventoryError, match="больше нуля"):
        await inventory.receive(session, product.id, north.id, qty)


async def test_receipt_to_deleted_warehouse_is_rejected(session):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")
    await inventory.delete_warehouse(session, north.id)

    with pytest.raises(inventory.InventoryError, match="удален"):
        await inventory.receive(session, product.id, north.id, 5)


async def test_repeated_receipt_request_is_applied_once(session, sessions):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")
    # откат после AlreadyDone сбрасывает загруженные объекты, id нужны заранее
    product_id, north_id, request_id = product.id, north.id, uuid4()

    await inventory.receive(session, product_id, north_id, 10, request_id=request_id)
    with pytest.raises(inventory.AlreadyDone):
        await inventory.receive(session, product_id, north_id, 10, request_id=request_id)

    stock = await stock_of(sessions, product_id, north_id)
    assert stock.on_hand == 10
    assert await session.scalar(select(func.count()).select_from(Operation)) == 1


async def test_warehouse_name_is_trimmed_and_must_be_unique(session):
    north = await inventory.create_warehouse(session, "  Север ")
    assert north.name == "Север"

    with pytest.raises(inventory.InventoryError, match="уже есть"):
        await inventory.create_warehouse(session, "север")


async def test_warehouse_name_cannot_be_empty(session):
    with pytest.raises(inventory.InventoryError, match="название"):
        await inventory.create_warehouse(session, "   ")


async def test_same_form_key_with_other_content_is_new_operation(session, sessions):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")
    product_id, north_id, request_id = product.id, north.id, uuid4()

    await inventory.receive(session, product_id, north_id, 1, request_id=request_id)
    await inventory.receive(session, product_id, north_id, 2, request_id=request_id)

    assert (await stock_of(sessions, product_id, north_id)).on_hand == 3


async def test_quantity_has_upper_limit(session):
    product = await make_product(session)
    north = await inventory.create_warehouse(session, "Север")

    with pytest.raises(inventory.InventoryError, match="Количество больше 1000000"):
        await inventory.receive(session, product.id, north.id, 3_000_000_000)


async def test_warehouse_name_has_length_limit(session):
    with pytest.raises(inventory.InventoryError, match="длиннее 100 символов"):
        await inventory.create_warehouse(session, "Ф" * 101)


async def test_repeated_warehouse_form_creates_one_warehouse(session):
    request_id = uuid4()
    await inventory.create_warehouse(session, "Восток", request_id=request_id)

    with pytest.raises(inventory.AlreadyDone):
        await inventory.create_warehouse(session, "Восток", request_id=request_id)
