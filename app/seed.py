"""Тестовые данные: ФФ Север и Юг, приход по трем товарам"""

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import inventory
from app.db import SessionLocal, engine
from app.models import Product

WAREHOUSES = ["Север", "Юг"]
TEST_DATA = [
    ("SKU-101", "Органайзер", {"Север": 10, "Юг": 6}),
    ("SKU-102", "Контейнер", {"Север": 5, "Юг": 0}),
    ("SKU-103", "Корзина", {"Север": 0, "Юг": 4}),
]

logger = logging.getLogger(__name__)


async def seed(session: AsyncSession) -> bool:
    """Заводит тестовые данные в пустую базу, False - данные уже есть"""
    async with session.begin():
        if await session.scalar(select(func.count()).select_from(Product)):
            return False
        products = {sku: Product(sku=sku, name=name) for sku, name, _ in TEST_DATA}
        session.add_all(products.values())

    warehouses = {name: await inventory.create_warehouse(session, name) for name in WAREHOUSES}
    for sku, _, quantities in TEST_DATA:
        for name, qty in quantities.items():
            # приход на 0 штук - не операция, ноль и так виден на экране остатков
            if qty:
                await inventory.receive(session, products[sku].id, warehouses[name].id, qty)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with SessionLocal() as session:
        added = await seed(session)
    await engine.dispose()
    logger.info("Тестовые данные добавлены" if added else "Тестовые данные уже есть")


if __name__ == "__main__":
    asyncio.run(main())
