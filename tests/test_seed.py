from sqlalchemy import func, select

from app.models import Operation, Product, Stock, Warehouse
from app.seed import seed


async def levels(session) -> dict[tuple[str, str], int]:
    rows = await session.execute(
        select(Product.sku, Warehouse.name, Stock.on_hand)
        .join(Stock, Stock.product_id == Product.id)
        .join(Warehouse, Warehouse.id == Stock.warehouse_id)
    )
    return {(sku, name): on_hand for sku, name, on_hand in rows}


async def test_seed_matches_test_data(session):
    await seed(session)

    assert await levels(session) == {
        ("SKU-101", "Север"): 10,
        ("SKU-101", "Юг"): 6,
        ("SKU-102", "Север"): 5,
        ("SKU-103", "Юг"): 4,
    }
    names = await session.scalars(select(Product.name).order_by(Product.sku))
    assert list(names) == ["Органайзер", "Контейнер", "Корзина"]


async def test_seed_skips_zero_quantities(session):
    await seed(session)

    assert await session.scalar(select(func.count()).select_from(Operation)) == 4


async def test_seed_runs_once(session):
    await seed(session)
    await seed(session)

    assert await session.scalar(select(func.count()).select_from(Product)) == 3
    assert await session.scalar(select(func.count()).select_from(Warehouse)) == 2
    assert await session.scalar(select(func.count()).select_from(Operation)) == 4
