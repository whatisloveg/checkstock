import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app import inventory
from app.config import settings
from app.models import Base, Product, Warehouse

# тесты чистят таблицы, поэтому всегда работают с отдельной базой; модули с подключением
# к базе (app.db и все, что его импортирует) загружаются только после этой строки
TEST_URL = make_url(settings.database_url).set(database="checkstock_test")
settings.database_url = TEST_URL.render_as_string(hide_password=False)


async def recreate_database() -> None:
    admin = create_async_engine(TEST_URL.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_URL.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{TEST_URL.database}"'))
    await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> None:
    asyncio.run(recreate_database())
    command.upgrade(Config("alembic.ini"), "head")


async def clean_tables(engine: AsyncEngine) -> None:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    await clean_tables(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessions() as session:
        yield session


@pytest.fixture
async def client(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    from app.db import get_session
    from app.main import app

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@dataclass
class Seeded:
    products: dict[str, int]
    warehouses: dict[str, int]


@pytest.fixture
async def data(sessions: async_sessionmaker[AsyncSession]) -> Seeded:
    from app.seed import seed

    async with sessions() as session:
        await seed(session)
        products = await session.execute(select(Product.sku, Product.id))
        warehouses = await session.execute(select(Warehouse.name, Warehouse.id))
        return Seeded(dict(products.all()), dict(warehouses.all()))


State = dict[str, dict[str, object]]


@pytest.fixture
def read_state(sessions: async_sessionmaker[AsyncSession]) -> Callable[[], Awaitable[State]]:
    # каждое чтение экрана остатков заодно сверяет остатки с историей
    async def read() -> State:
        async with sessions() as session:
            await inventory.use_snapshot(session)
            overview = await inventory.stock_overview(session)
            assert await inventory.reconcile(session) == []
        state: State = {}
        for row in overview.products:
            item: dict[str, object] = {
                warehouse.name: (cell.on_hand, cell.reserved, cell.available)
                for warehouse in overview.warehouses
                for cell in [row.cell(warehouse.id)]
            }
            item["в пути между ФФ"] = row.between_warehouses
            item["FBO в пути"] = row.fbo_in_transit
            item["FBO принято"] = row.fbo_accepted
            item["FBS к клиенту"] = row.fbs_to_client
            state[row.product.sku] = item
        return state

    return read
