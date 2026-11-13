from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine
from app.models import Operation, OperationKind, Product, Stock, Transfer, Warehouse


async def add_product_and_warehouses(session: AsyncSession) -> tuple[Product, Warehouse, Warehouse]:
    product = Product(sku="SKU-1", name="Товар")
    north, south = Warehouse(name="Север"), Warehouse(name="Юг")
    session.add_all([product, north, south])
    await session.commit()
    return product, north, south


async def test_stock_cannot_go_negative(session):
    product, north, _ = await add_product_and_warehouses(session)
    session.add(Stock(product_id=product.id, warehouse_id=north.id, on_hand=-1))
    with pytest.raises(IntegrityError, match="ck_stock_on_hand_not_negative"):
        await session.commit()


async def test_reserve_cannot_exceed_physical_stock(session):
    product, north, _ = await add_product_and_warehouses(session)
    session.add(Stock(product_id=product.id, warehouse_id=north.id, on_hand=2, reserved=3))
    with pytest.raises(IntegrityError, match="ck_stock_reserved_within_on_hand"):
        await session.commit()


async def test_transfer_to_same_warehouse_is_rejected(session):
    product, north, _ = await add_product_and_warehouses(session)
    session.add(
        Transfer(product_id=product.id, from_warehouse_id=north.id, to_warehouse_id=north.id, qty=1)
    )
    with pytest.raises(IntegrityError, match="ck_transfers_different_warehouses"):
        await session.commit()


async def test_operation_quantity_must_be_positive(session):
    product, north, _ = await add_product_and_warehouses(session)
    receipt = Operation(
        kind=OperationKind.RECEIPT, product_id=product.id, to_warehouse_id=north.id, qty=0
    )
    session.add(receipt)
    with pytest.raises(IntegrityError, match="ck_operations_qty_positive"):
        await session.commit()


async def test_unknown_status_is_rejected(session):
    product, north, south = await add_product_and_warehouses(session)
    with pytest.raises(IntegrityError, match="ck_transfers_transfer_status"):
        await session.execute(
            text(
                "INSERT INTO transfers"
                " (product_id, from_warehouse_id, to_warehouse_id, qty, status)"
                " VALUES (:p, :f, :t, 1, 'lost')"
            ),
            {"p": product.id, "f": north.id, "t": south.id},
        )


async def test_active_warehouse_names_are_unique_ignoring_case(session):
    await add_product_and_warehouses(session)
    session.add(Warehouse(name="СЕВЕР"))
    with pytest.raises(IntegrityError, match="uq_warehouses_active_name"):
        await session.commit()


async def test_name_of_deleted_warehouse_can_be_reused(session):
    _, north, _ = await add_product_and_warehouses(session)
    north.deleted_at = datetime.now(UTC)
    await session.commit()
    session.add(Warehouse(name="Север"))
    await session.commit()


def test_app_itself_uses_test_database():
    assert engine.url.database == "checkstock_test"
