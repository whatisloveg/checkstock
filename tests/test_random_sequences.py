import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import inventory
from app.config import settings as app_settings
from app.inventory import Allocation
from app.models import FboShipment, FbsOrder, Operation, Product, Transfer, Warehouse
from app.seed import TEST_DATA, seed
from tests.conftest import clean_tables

product = st.sampled_from(["SKU-101", "SKU-102", "SKU-103"])
warehouse = st.sampled_from(["Север", "Юг"])
order = st.sampled_from(["R-1", "R-2"])
qty = st.integers(1, 7)
pick = st.integers(0, 20)

steps = st.lists(
    st.one_of(
        st.tuples(st.just("receive"), product, warehouse, qty),
        st.tuples(st.just("transfer"), product, warehouse, warehouse, qty),
        st.tuples(st.just("receive_transfer"), pick),
        st.tuples(st.just("reserve"), order, product, warehouse, qty),
        st.tuples(st.just("reserve_split"), order, product, qty, qty),
        st.tuples(st.just("ship"), pick),
        st.tuples(st.just("cancel"), pick),
        st.tuples(st.just("fbo"), product, warehouse, qty),
        st.tuples(st.just("accept_fbo"), pick),
        st.tuples(st.just("delete"), warehouse),
        st.tuples(st.just("repeat")),
    ),
    # на коротких цепочках перемещение редко доходит до приемки
    min_size=10,
    max_size=30,
)
CREATES = {"receive", "transfer", "reserve", "reserve_split", "fbo"}


@dataclass
class Doc:
    sku: str
    ff: str
    qty: int
    status: str
    target: str = ""
    order: str = ""


# тот же учет, написанный заново отдельно от сервиса, каждый шаг сверяется с ним
@dataclass
class Model:
    stock: defaultdict[tuple[str, str], list[int]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0])
    )
    deleted: set[str] = field(default_factory=set)
    transfers: list[Doc] = field(default_factory=list)
    orders: list[Doc] = field(default_factory=list)
    shipments: list[Doc] = field(default_factory=list)

    def available(self, sku: str, ff: str) -> int:
        on_hand, reserved = self.stock[sku, ff]
        return on_hand - reserved

    def can_take(self, sku: str, parts: list[tuple[str, int]]) -> bool:
        return all(ff not in self.deleted and self.available(sku, ff) >= n for ff, n in parts)

    def apply(self, name: str, args: list[Any]) -> bool:
        match name:
            case "receive":
                sku, ff, n = args
                if ff in self.deleted:
                    return False
                self.stock[sku, ff][0] += n
            case "transfer":
                sku, source, target, n = args
                if source == target or target in self.deleted or self.available(sku, source) < n:
                    return False
                self.stock[sku, source][0] -= n
                self.transfers.append(Doc(sku, source, n, "in_transit", target=target))
            case "receive_transfer":
                doc = pick_doc(self.transfers, args[0])
                if doc is None or doc.status != "in_transit":
                    return False
                self.stock[doc.sku, doc.target][0] += doc.qty
                doc.status = "received"
            case "reserve" | "reserve_split":
                number, sku, *rest = args
                parts = (
                    [(rest[0], rest[1])]
                    if name == "reserve"
                    else list(zip(["Север", "Юг"], rest, strict=True))
                )
                taken = any(
                    d.order == number and d.sku == sku and d.status != "cancelled"
                    for d in self.orders
                )
                if taken or not self.can_take(sku, parts):
                    return False
                for ff, n in parts:
                    self.stock[sku, ff][1] += n
                    self.orders.append(Doc(sku, ff, n, "reserved", order=number))
            case "ship" | "cancel":
                doc = pick_doc(self.orders, args[0])
                if doc is None or doc.status != "reserved":
                    return False
                if name == "ship":
                    self.stock[doc.sku, doc.ff][0] -= doc.qty
                self.stock[doc.sku, doc.ff][1] -= doc.qty
                doc.status = "shipped" if name == "ship" else "cancelled"
            case "fbo":
                sku, ff, n = args
                if not self.can_take(sku, [(ff, n)]):
                    return False
                self.stock[sku, ff][0] -= n
                self.shipments.append(Doc(sku, ff, n, "in_transit"))
            case "accept_fbo":
                doc = pick_doc(self.shipments, args[0])
                if doc is None or doc.status != "in_transit":
                    return False
                doc.status = "accepted"
            case "delete":
                if args[0] in self.deleted:
                    return False
                self.deleted.add(args[0])
        return True

    def expected(self) -> dict[str, object]:
        def total(docs: list[Doc], sku: str, status: str) -> int:
            return sum(d.qty for d in docs if d.sku == sku and d.status == status)

        return {
            sku: (
                {ff: tuple(self.stock[sku, ff]) for ff in ("Север", "Юг")},
                total(self.transfers, sku, "in_transit"),
                total(self.shipments, sku, "in_transit"),
                total(self.shipments, sku, "accepted"),
                total(self.orders, sku, "shipped"),
            )
            for sku, _, _ in TEST_DATA
        }


def pick_doc(docs: list[Doc], index: int) -> Doc | None:
    return docs[index % len(docs)] if docs else None


async def observe(sessions: async_sessionmaker[AsyncSession], ffs: dict[str, int]) -> object:
    async with sessions() as session:
        overview = await inventory.stock_overview(session)
    return {
        row.product.sku: (
            {name: (row.cell(id_).on_hand, row.cell(id_).reserved) for name, id_ in ffs.items()},
            row.between_warehouses,
            row.fbo_in_transit,
            row.fbo_accepted,
            row.fbs_to_client,
        )
        for row in overview.products
    }


async def count_operations(sessions: async_sessionmaker[AsyncSession]) -> int | None:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(Operation))


async def play(actions: list[tuple[Any, ...]]) -> None:
    engine = create_async_engine(app_settings.database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await clean_tables(engine)
    async with sessions() as session:
        await seed(session)
        products = dict((await session.execute(select(Product.sku, Product.id))).all())
        ffs = dict((await session.execute(select(Warehouse.name, Warehouse.id))).all())

    model = Model()
    for sku, _, quantities in TEST_DATA:
        for ff, n in quantities.items():
            model.stock[sku, ff][0] = n
    last: tuple[str, list[Any], UUID] | None = None

    for action in actions:
        name, *args = action
        key = uuid4()
        if name == "repeat":
            if last is None:
                continue
            # та же форма с тем же ключом второй раз
            name, args, key = last
            should_pass = False
        else:
            should_pass = model.apply(name, args)

        before = await observe(sessions, ffs), await count_operations(sessions)
        try:
            await run(sessions, name, args, products, ffs, key)
            passed = True
        except inventory.InventoryError:
            passed = False
            assert (await observe(sessions, ffs), await count_operations(sessions)) == before
        assert passed == should_pass, action
        if passed and name in CREATES:
            last = name, args, key

        assert await observe(sessions, ffs) == model.expected(), action
        async with sessions() as session:
            assert await inventory.reconcile(session) == [], action
    await engine.dispose()


async def run(sessions, name, args, products, ffs, key) -> None:
    async def nth(model: type[Transfer | FbsOrder | FboShipment]) -> int:
        async with sessions() as reader:
            ids: list[int] = list(await reader.scalars(select(model.id).order_by(model.id)))
        if not ids:
            raise inventory.InventoryError("документов еще нет")
        return ids[int(args[0]) % len(ids)]

    async with sessions() as session:
        match name:
            case "receive":
                await inventory.receive(session, products[args[0]], ffs[args[1]], args[2], key)
            case "transfer":
                source, target = ffs[args[1]], ffs[args[2]]
                await inventory.send_transfer(
                    session, products[args[0]], source, target, args[3], key
                )
            case "receive_transfer":
                await inventory.receive_transfer(session, await nth(Transfer))
            case "reserve":
                parts = [Allocation(ffs[args[2]], args[3])]
                await inventory.reserve_fbs(session, args[0], products[args[1]], parts, key)
            case "reserve_split":
                parts = [Allocation(ffs["Север"], args[2]), Allocation(ffs["Юг"], args[3])]
                await inventory.reserve_fbs(session, args[0], products[args[1]], parts, key)
            case "ship":
                await inventory.ship_fbs(session, await nth(FbsOrder))
            case "cancel":
                await inventory.cancel_fbs(session, await nth(FbsOrder))
            case "fbo":
                await inventory.send_fbo(
                    session, products[args[0]], [Allocation(ffs[args[1]], args[2])], key
                )
            case "accept_fbo":
                await inventory.accept_fbo(session, await nth(FboShipment))
            case "delete":
                await inventory.delete_warehouse(session, ffs[args[0]])


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps)
def test_random_sequences_match_independent_model(actions):
    asyncio.run(play(actions))
