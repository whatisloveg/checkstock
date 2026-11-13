"""Учет товара на ФФ, все изменения остатков проходят через этот модуль"""

from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ORDER_NUMBER_LENGTH,
    WAREHOUSE_NAME_LENGTH,
    FboShipment,
    FboStatus,
    FbsOrder,
    FbsStatus,
    Operation,
    OperationKind,
    ProcessedRequest,
    Product,
    Stock,
    Transfer,
    TransferStatus,
    Warehouse,
)

# больше миллиона штук за раз - скорее штрихкод, отсканированный в поле количества
MAX_QTY = 1_000_000


class InventoryError(Exception):
    """Действие нарушает правила учета, текст ошибки показывается менеджеру"""


class AlreadyDone(InventoryError):
    """Операция уже проведена, повтор ничего не меняет"""


@dataclass(frozen=True)
class Allocation:
    """Сколько товара взять с одного ФФ"""

    warehouse_id: int
    qty: int


class Shortage(InventoryError):
    """На выбранном ФФ не хватает доступного товара"""

    def __init__(self, message: str, split: list[Allocation] | None) -> None:
        super().__init__(message)
        self.split = split


async def create_warehouse(
    session: AsyncSession, name: str, request_id: UUID | None = None
) -> Warehouse:
    """Заводит ФФ, название уникально среди действующих"""
    warehouse = Warehouse(name=clean_name(name))
    async with unique_name_guard(warehouse.name), session.begin():
        await claim_request(session, request_id, warehouse.name)
        session.add(warehouse)
    return warehouse


async def rename_warehouse(session: AsyncSession, warehouse_id: int, name: str) -> Warehouse:
    """Меняет название действующего ФФ, история ссылается на ФФ по id и не меняется"""
    name = clean_name(name)
    async with unique_name_guard(name), session.begin():
        warehouse = await lock_warehouse(session, warehouse_id)
        if warehouse.deleted_at is not None:
            raise InventoryError(f"ФФ {warehouse.name} удален, переименовать нельзя")
        warehouse.name = name
    return warehouse


async def delete_warehouse(session: AsyncSession, warehouse_id: int) -> Warehouse:
    """Помечает ФФ удаленным, остатки и история остаются"""
    async with session.begin():
        warehouse = await lock_warehouse(session, warehouse_id)
        if warehouse.deleted_at is not None:
            raise AlreadyDone(f"ФФ {warehouse.name} уже удален")
        warehouse.deleted_at = datetime.now(UTC)
    return warehouse


async def receive(
    session: AsyncSession,
    product_id: int,
    warehouse_id: int,
    qty: int,
    request_id: UUID | None = None,
) -> None:
    """Приход товара на ФФ"""
    check_qty(qty)
    async with session.begin():
        await claim_request(session, request_id, product_id, warehouse_id, qty)
        await get_product(session, product_id)
        await get_active_warehouse(session, warehouse_id, "приход на него невозможен")
        stock = (await lock_stock(session, product_id, [warehouse_id]))[warehouse_id]
        stock.on_hand += qty
        session.add(
            Operation(
                kind=OperationKind.RECEIPT,
                product_id=product_id,
                qty=qty,
                to_warehouse_id=warehouse_id,
            )
        )


async def send_transfer(
    session: AsyncSession,
    product_id: int,
    from_warehouse_id: int,
    to_warehouse_id: int,
    qty: int,
    request_id: UUID | None = None,
) -> Transfer:
    """Отправляет товар на другой ФФ, до приемки он числится в пути"""
    check_qty(qty)
    if from_warehouse_id == to_warehouse_id:
        raise InventoryError("Перемещать на тот же ФФ нельзя")
    async with session.begin():
        await claim_request(
            session, request_id, product_id, from_warehouse_id, to_warehouse_id, qty
        )
        await get_product(session, product_id)
        # с удаленного ФФ товар можно вывезти, а завезти на него нельзя
        await get_warehouse(session, from_warehouse_id)
        await get_active_warehouse(session, to_warehouse_id, "перемещать на него нельзя")
        stock = (await lock_stock(session, product_id, [from_warehouse_id]))[from_warehouse_id]
        if stock.available < qty:
            raise await make_shortage(session, product_id, from_warehouse_id, qty, can_split=False)
        stock.on_hand -= qty
        transfer = Transfer(
            product_id=product_id,
            from_warehouse_id=from_warehouse_id,
            to_warehouse_id=to_warehouse_id,
            qty=qty,
        )
        session.add(transfer)
        await session.flush()
        session.add(
            Operation(
                kind=OperationKind.TRANSFER_SENT,
                product_id=product_id,
                qty=qty,
                from_warehouse_id=from_warehouse_id,
                to_warehouse_id=to_warehouse_id,
                transfer_id=transfer.id,
            )
        )
    return transfer


async def receive_transfer(session: AsyncSession, transfer_id: int) -> Transfer:
    """Принимает перемещение на ФФ назначения"""
    async with session.begin():
        # блокировка строки документа делает повторное подтверждение безопасным
        transfer = await session.get(
            Transfer, transfer_id, with_for_update=True, populate_existing=True
        )
        if transfer is None:
            raise InventoryError("Перемещение не найдено")
        if transfer.status == TransferStatus.RECEIVED:
            raise AlreadyDone("Перемещение уже принято")
        target = transfer.to_warehouse_id
        stock = (await lock_stock(session, transfer.product_id, [target]))[target]
        stock.on_hand += transfer.qty
        transfer.status = TransferStatus.RECEIVED
        transfer.received_at = datetime.now(UTC)
        session.add(
            Operation(
                kind=OperationKind.TRANSFER_RECEIVED,
                product_id=transfer.product_id,
                qty=transfer.qty,
                from_warehouse_id=transfer.from_warehouse_id,
                to_warehouse_id=target,
                transfer_id=transfer.id,
            )
        )
    return transfer


async def reserve_fbs(
    session: AsyncSession,
    order_number: str,
    product_id: int,
    allocations: list[Allocation],
    request_id: UUID | None = None,
) -> list[FbsOrder]:
    """Резервирует товар под заказ FBS, при разделении все части создаются вместе"""
    order_number = order_number.strip()
    if not order_number:
        raise InventoryError("Укажите номер заказа")
    if len(order_number) > ORDER_NUMBER_LENGTH:
        raise InventoryError(f"Номер заказа длиннее {ORDER_NUMBER_LENGTH} символов")
    check_allocations(allocations)
    async with session.begin():
        await claim_request(session, request_id, order_number, product_id, allocations)
        product = await get_product(session, product_id)
        await check_order_is_new(session, order_number, product)
        for allocation in allocations:
            await get_active_warehouse(
                session, allocation.warehouse_id, "резервировать на нем нельзя"
            )
        stocks = await lock_available(session, product_id, allocations)
        orders = []
        for allocation in allocations:
            stocks[allocation.warehouse_id].reserved += allocation.qty
            order = FbsOrder(
                order_number=order_number,
                product_id=product_id,
                warehouse_id=allocation.warehouse_id,
                qty=allocation.qty,
            )
            session.add(order)
            orders.append(order)
        await session.flush()
        for order in orders:
            session.add(
                Operation(
                    kind=OperationKind.FBS_RESERVED,
                    product_id=product_id,
                    qty=order.qty,
                    from_warehouse_id=order.warehouse_id,
                    to_warehouse_id=order.warehouse_id,
                    fbs_order_id=order.id,
                )
            )
    return orders


async def ship_fbs(session: AsyncSession, order_id: int) -> FbsOrder:
    """Отправляет заказ FBS клиенту, резерв и физический остаток уменьшаются"""
    async with session.begin():
        order = await lock_fbs_order(session, order_id)
        if order.status == FbsStatus.SHIPPED:
            raise AlreadyDone(f"Заказ {order.order_number} уже отправлен")
        if order.status == FbsStatus.CANCELLED:
            raise InventoryError(f"Заказ {order.order_number} отменен, отправить его нельзя")
        stock = (await lock_stock(session, order.product_id, [order.warehouse_id]))[
            order.warehouse_id
        ]
        stock.on_hand -= order.qty
        stock.reserved -= order.qty
        close_fbs_order(session, order, FbsStatus.SHIPPED, OperationKind.FBS_SHIPPED)
    return order


async def cancel_fbs(session: AsyncSession, order_id: int) -> FbsOrder:
    """Отменяет заказ FBS, товар снова доступен"""
    async with session.begin():
        order = await lock_fbs_order(session, order_id)
        if order.status == FbsStatus.CANCELLED:
            raise AlreadyDone(f"Заказ {order.order_number} уже отменен")
        if order.status == FbsStatus.SHIPPED:
            raise InventoryError(f"Заказ {order.order_number} уже отправлен, отменить нельзя")
        stock = (await lock_stock(session, order.product_id, [order.warehouse_id]))[
            order.warehouse_id
        ]
        stock.reserved -= order.qty
        close_fbs_order(session, order, FbsStatus.CANCELLED, OperationKind.FBS_CANCELLED)
    return order


async def send_fbo(
    session: AsyncSession,
    product_id: int,
    allocations: list[Allocation],
    request_id: UUID | None = None,
) -> list[FboShipment]:
    """Отгружает товар на склад маркетплейса, до приемки он числится в пути"""
    check_allocations(allocations)
    async with session.begin():
        await claim_request(session, request_id, product_id, allocations)
        await get_product(session, product_id)
        for allocation in allocations:
            await get_active_warehouse(
                session,
                allocation.warehouse_id,
                "с него товар можно только переместить на другой ФФ",
            )
        stocks = await lock_available(session, product_id, allocations)
        shipments = []
        for allocation in allocations:
            stocks[allocation.warehouse_id].on_hand -= allocation.qty
            shipment = FboShipment(
                product_id=product_id, warehouse_id=allocation.warehouse_id, qty=allocation.qty
            )
            session.add(shipment)
            shipments.append(shipment)
        await session.flush()
        for shipment in shipments:
            session.add(
                Operation(
                    kind=OperationKind.FBO_SENT,
                    product_id=product_id,
                    qty=shipment.qty,
                    from_warehouse_id=shipment.warehouse_id,
                    fbo_shipment_id=shipment.id,
                )
            )
    return shipments


async def accept_fbo(session: AsyncSession, shipment_id: int) -> FboShipment:
    """Отмечает приемку поставки складом маркетплейса"""
    async with session.begin():
        shipment = await session.get(
            FboShipment, shipment_id, with_for_update=True, populate_existing=True
        )
        if shipment is None:
            raise InventoryError("Поставка не найдена")
        if shipment.status == FboStatus.ACCEPTED:
            raise AlreadyDone("Поставка уже принята маркетплейсом")
        shipment.status = FboStatus.ACCEPTED
        shipment.accepted_at = datetime.now(UTC)
        session.add(
            Operation(
                kind=OperationKind.FBO_ACCEPTED,
                product_id=shipment.product_id,
                qty=shipment.qty,
                from_warehouse_id=shipment.warehouse_id,
                fbo_shipment_id=shipment.id,
            )
        )
    return shipment


@dataclass
class Cell:
    on_hand: int = 0
    reserved: int = 0

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass
class ProductStock:
    product: Product
    cells: dict[int, Cell] = field(default_factory=dict)
    between_warehouses: int = 0
    fbo_in_transit: int = 0
    fbo_accepted: int = 0
    fbs_to_client: int = 0

    def cell(self, warehouse_id: int) -> Cell:
        return self.cells.get(warehouse_id, Cell())


@dataclass
class Overview:
    warehouses: list[Warehouse]
    products: list[ProductStock]


async def use_snapshot(session: AsyncSession) -> None:
    """Все чтения до конца транзакции видят один снимок базы"""
    # экран читает несколько таблиц, без снимка чужое перемещение между запросами
    # покажет товар дважды или ни разу
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})


async def stock_overview(session: AsyncSession) -> Overview:
    """Экран остатков: все товары по каждому ФФ и отдельно товар в пути и у клиента"""
    products = {
        product.id: ProductStock(product)
        for product in await session.scalars(select(Product).order_by(Product.sku))
    }
    for stock in await session.scalars(select(Stock)):
        products[stock.product_id].cells[stock.warehouse_id] = Cell(stock.on_hand, stock.reserved)

    in_transit = select(Transfer.product_id, func.sum(Transfer.qty)).where(
        Transfer.status == TransferStatus.IN_TRANSIT
    )
    for product_id, qty in await session.execute(in_transit.group_by(Transfer.product_id)):
        products[product_id].between_warehouses = qty
    fbo = select(FboShipment.product_id, FboShipment.status, func.sum(FboShipment.qty))
    fbo = fbo.group_by(FboShipment.product_id, FboShipment.status)
    for product_id, status, qty in await session.execute(fbo):
        if status == FboStatus.IN_TRANSIT:
            products[product_id].fbo_in_transit = qty
        else:
            products[product_id].fbo_accepted = qty
    shipped = select(FbsOrder.product_id, func.sum(FbsOrder.qty)).where(
        FbsOrder.status == FbsStatus.SHIPPED
    )
    for product_id, qty in await session.execute(shipped.group_by(FbsOrder.product_id)):
        products[product_id].fbs_to_client = qty

    # удаленный ФФ остается на экране, пока на нем числится товар
    holding = {
        warehouse_id
        for row in products.values()
        for warehouse_id, cell in row.cells.items()
        if cell.on_hand
    }
    warehouses = [
        warehouse
        for warehouse in await session.scalars(
            select(Warehouse).order_by(Warehouse.deleted_at.is_not(None), Warehouse.id)
        )
        if warehouse.deleted_at is None or warehouse.id in holding
    ]
    return Overview(warehouses, list(products.values()))


async def reconcile(session: AsyncSession) -> list[str]:
    """Расхождения остатков с историей операций, пустой список - все сходится"""
    names = dict((await session.execute(select(Warehouse.id, Warehouse.name))).all())
    skus = dict((await session.execute(select(Product.id, Product.sku))).all())

    kind = Operation.kind
    warehouse = case(
        (
            kind.in_([OperationKind.RECEIPT, OperationKind.TRANSFER_RECEIVED]),
            Operation.to_warehouse_id,
        ),
        else_=Operation.from_warehouse_id,
    )
    on_hand = case(
        (kind.in_([OperationKind.RECEIPT, OperationKind.TRANSFER_RECEIVED]), Operation.qty),
        (
            kind.in_(
                [OperationKind.TRANSFER_SENT, OperationKind.FBO_SENT, OperationKind.FBS_SHIPPED]
            ),
            -Operation.qty,
        ),
        else_=0,
    )
    reserved = case(
        (kind == OperationKind.FBS_RESERVED, Operation.qty),
        (kind.in_([OperationKind.FBS_SHIPPED, OperationKind.FBS_CANCELLED]), -Operation.qty),
        else_=0,
    )
    expected: dict[tuple[int, int], tuple[int, int]] = defaultdict(lambda: (0, 0))
    by_history = select(
        Operation.product_id, warehouse, func.sum(on_hand), func.sum(reserved)
    ).group_by(Operation.product_id, warehouse)
    for product_id, warehouse_id, qty, held in await session.execute(by_history):
        if warehouse_id is not None:
            expected[product_id, warehouse_id] = (qty, held)

    problems = []
    actual = {(s.product_id, s.warehouse_id): s for s in await session.scalars(select(Stock))}
    for key in sorted(actual.keys() | expected.keys()):
        stock = actual.get(key)
        fact = (stock.on_hand, stock.reserved) if stock else (0, 0)
        place = f"{skus[key[0]]}, ФФ {names[key[1]]}"
        if fact[0] != expected[key][0]:
            problems.append(f"{place}: физически {fact[0]}, по истории {expected[key][0]}")
        if fact[1] != expected[key][1]:
            problems.append(f"{place}: в резерве {fact[1]}, по истории {expected[key][1]}")

    # каждая единица, которая пришла, должна быть где-то: на ФФ, в пути, на МП или у клиента
    overview = await stock_overview(session)
    receipts = (
        select(Operation.product_id, func.sum(Operation.qty))
        .where(kind == OperationKind.RECEIPT)
        .group_by(Operation.product_id)
    )
    received = dict((await session.execute(receipts)).all())
    for row in overview.products:
        accounted = (
            sum(cell.on_hand for cell in row.cells.values())
            + row.between_warehouses
            + row.fbo_in_transit
            + row.fbo_accepted
            + row.fbs_to_client
        )
        came = received.get(row.product.id, 0)
        if came != accounted:
            problems.append(f"{row.product.sku}: пришло {came}, учтено {accounted}")
    return problems


def clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise InventoryError("Укажите название ФФ")
    if len(name) > WAREHOUSE_NAME_LENGTH:
        raise InventoryError(f"Название ФФ длиннее {WAREHOUSE_NAME_LENGTH} символов")
    return name


@asynccontextmanager
async def unique_name_guard(name: str) -> AsyncIterator[None]:
    """Превращает нарушение уникальности названия ФФ в понятную ошибку"""
    try:
        yield
    except IntegrityError as error:
        if "uq_warehouses_active_name" not in str(error.orig):
            raise
        raise InventoryError(f"ФФ с названием {name} уже есть") from error


def check_qty(qty: int) -> None:
    if qty <= 0:
        raise InventoryError("Количество должно быть больше нуля")
    if qty > MAX_QTY:
        raise InventoryError(f"Количество больше {MAX_QTY}, проверьте ввод")


def check_allocations(allocations: list[Allocation]) -> None:
    if not allocations:
        raise InventoryError("Выберите ФФ")
    for allocation in allocations:
        check_qty(allocation.qty)
    if len({allocation.warehouse_id for allocation in allocations}) < len(allocations):
        raise InventoryError("Один ФФ указан дважды")


async def claim_request(session: AsyncSession, request_id: UUID | None, *content: object) -> None:
    """Запоминает ключ формы вместе с ее содержимым, на повторе бросает AlreadyDone"""
    if request_id is None:
        return
    # устаревшая страница с другими данными - новая операция, а не повтор
    key = uuid5(request_id, repr(content))
    # повтор, пришедший параллельно, ждет здесь фиксации первого запроса
    claimed = await session.scalar(
        insert(ProcessedRequest)
        .values(id=key)
        .on_conflict_do_nothing()
        .returning(ProcessedRequest.id)
    )
    if claimed is None:
        raise AlreadyDone("Эта операция уже проведена")


async def get_product(session: AsyncSession, product_id: int) -> Product:
    product = await session.get(Product, product_id)
    if product is None:
        raise InventoryError("Товар не найден")
    return product


async def check_order_is_new(session: AsyncSession, order_number: str, product: Product) -> None:
    """Отказывает, если этот товар по заказу уже зарезервирован или отправлен"""
    # два резерва одного заказа идут по очереди, иначе ни один не увидит другой
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(order_number))))
    status = await session.scalar(
        select(FbsOrder.status).where(
            FbsOrder.order_number == order_number,
            FbsOrder.product_id == product.id,
            FbsOrder.status != FbsStatus.CANCELLED,
        )
    )
    if status is not None:
        done = "отправлен" if status == FbsStatus.SHIPPED else "зарезервирован"
        raise InventoryError(f"Заказ {order_number} по {product.sku} уже {done}")


async def get_warehouse(session: AsyncSession, warehouse_id: int) -> Warehouse:
    # FOR SHARE не дает удалить ФФ, пока по нему идет операция
    warehouse = await session.get(
        Warehouse, warehouse_id, with_for_update={"read": True}, populate_existing=True
    )
    if warehouse is None:
        raise InventoryError("ФФ не найден")
    return warehouse


async def get_active_warehouse(session: AsyncSession, warehouse_id: int, why: str) -> Warehouse:
    warehouse = await get_warehouse(session, warehouse_id)
    if warehouse.deleted_at is not None:
        raise InventoryError(f"ФФ {warehouse.name} удален, {why}")
    return warehouse


async def lock_warehouse(session: AsyncSession, warehouse_id: int) -> Warehouse:
    warehouse = await session.get(
        Warehouse, warehouse_id, with_for_update=True, populate_existing=True
    )
    if warehouse is None:
        raise InventoryError("ФФ не найден")
    return warehouse


async def lock_fbs_order(session: AsyncSession, order_id: int) -> FbsOrder:
    # блокировка строки заказа делает повторное подтверждение безопасным
    order = await session.get(FbsOrder, order_id, with_for_update=True, populate_existing=True)
    if order is None:
        raise InventoryError("Заказ не найден")
    return order


def close_fbs_order(
    session: AsyncSession, order: FbsOrder, status: FbsStatus, kind: OperationKind
) -> None:
    order.status = status
    order.closed_at = datetime.now(UTC)
    session.add(
        Operation(
            kind=kind,
            product_id=order.product_id,
            qty=order.qty,
            from_warehouse_id=order.warehouse_id,
            to_warehouse_id=order.warehouse_id if kind == OperationKind.FBS_CANCELLED else None,
            fbs_order_id=order.id,
        )
    )


async def lock_stock(
    session: AsyncSession, product_id: int, warehouse_ids: list[int]
) -> dict[int, Stock]:
    """Строки остатка под блокировкой, недостающие создаются с нулями"""
    ids = sorted(set(warehouse_ids))
    await session.execute(
        insert(Stock)
        .values([{"product_id": product_id, "warehouse_id": id_} for id_ in ids])
        .on_conflict_do_nothing()
    )
    # один порядок блокировок во всех операциях исключает взаимную блокировку
    rows = await session.scalars(
        select(Stock)
        .where(Stock.product_id == product_id, Stock.warehouse_id.in_(ids))
        .order_by(Stock.warehouse_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {row.warehouse_id: row for row in rows}


async def lock_available(
    session: AsyncSession, product_id: int, allocations: list[Allocation]
) -> dict[int, Stock]:
    """Блокирует остатки и проверяет, что на каждом ФФ хватает доступного товара"""
    stocks = await lock_stock(session, product_id, [a.warehouse_id for a in allocations])
    if any(stocks[a.warehouse_id].available < a.qty for a in allocations):
        total = sum(allocation.qty for allocation in allocations)
        raise await make_shortage(session, product_id, allocations[0].warehouse_id, total)
    return stocks


async def make_shortage(
    session: AsyncSession,
    product_id: int,
    warehouse_id: int,
    requested: int,
    can_split: bool = True,
) -> Shortage:
    """Ошибка нехватки с остатками на других ФФ и вариантом разделения"""
    available = Stock.on_hand - Stock.reserved
    rows = await session.execute(
        select(Warehouse.id, Warehouse.name, Warehouse.deleted_at, func.coalesce(available, 0))
        .outerjoin(Stock, (Stock.warehouse_id == Warehouse.id) & (Stock.product_id == product_id))
        .order_by(func.coalesce(available, 0).desc(), Warehouse.id)
    )
    here, name = 0, ""
    others: list[tuple[int, str, int]] = []
    deleted: list[tuple[int, str, int]] = []
    for id_, warehouse_name, deleted_at, qty in rows:
        if id_ == warehouse_id:
            here, name = qty, warehouse_name
        elif qty > 0:
            # с удаленного ФФ товар можно только переместить, в разделение он не идет
            (deleted if deleted_at else others).append((id_, warehouse_name, qty))

    message = f"На ФФ {name} доступно {here} из {requested}, не хватает {requested - here}. "
    message += f"На других ФФ: {listing(others)}" if others else "На других ФФ товара нет"
    if deleted:
        message += f". На удаленных ФФ, только перемещением: {listing(deleted)}"

    split = None
    if can_split and here + sum(qty for _, _, qty in others) >= requested:
        split = [Allocation(warehouse_id, here)] if here else []
        rest = requested - here
        for id_, _, qty in others:
            if rest == 0:
                break
            split.append(Allocation(id_, min(qty, rest)))
            rest -= min(qty, rest)
    return Shortage(message, split)


def listing(warehouses: list[tuple[int, str, int]]) -> str:
    return ", ".join(f"{name}: {qty}" for _, name, qty in warehouses)
