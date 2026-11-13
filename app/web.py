"""Страницы и формы менеджера"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException

from app import inventory
from app.db import get_session
from app.inventory import Allocation, AlreadyDone, InventoryError, Shortage
from app.models import (
    ORDER_NUMBER_LENGTH,
    WAREHOUSE_NAME_LENGTH,
    FboShipment,
    FboStatus,
    FbsOrder,
    FbsStatus,
    Operation,
    OperationKind,
    Product,
    Stock,
    Transfer,
    TransferStatus,
    Warehouse,
)

# склады в одном часовом поясе, время на экранах московское
MOSCOW = ZoneInfo("Europe/Moscow")

NAV = [
    ("/", "Остатки"),
    ("/receipts", "Приход"),
    ("/transfers", "Перемещения"),
    ("/fbs", "FBS"),
    ("/fbo", "FBO"),
    ("/warehouses", "ФФ"),
    ("/history", "История"),
]
CELLS = ["Физически", "Доступно", "Резерв FBS"]
OUTSIDE = [
    ("between_warehouses", "В пути между ФФ"),
    ("fbo_in_transit", "FBO в пути"),
    ("fbo_accepted", "FBO принято"),
    ("fbs_to_client", "FBS к клиенту"),
]
KINDS = {
    OperationKind.RECEIPT: "Приход",
    OperationKind.TRANSFER_SENT: "Перемещение: отправка",
    OperationKind.TRANSFER_RECEIVED: "Перемещение: приемка",
    OperationKind.FBO_SENT: "FBO: отгрузка",
    OperationKind.FBO_ACCEPTED: "FBO: приемка маркетплейсом",
    OperationKind.FBS_RESERVED: "FBS: резерв",
    OperationKind.FBS_SHIPPED: "FBS: отправка клиенту",
    OperationKind.FBS_CANCELLED: "FBS: отмена резерва",
}


def ff_label(warehouse: Warehouse) -> str:
    return f"{warehouse.name} (удален)" if warehouse.deleted_at else warehouse.name


def when(moment: datetime) -> str:
    return moment.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


router = APIRouter(default_response_class=HTMLResponse)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(
    nav=NAV,
    cells=CELLS,
    outside=OUTSIDE,
    ff_label=ff_label,
    when=when,
    max_qty=inventory.MAX_QTY,
    name_length=WAREHOUSE_NAME_LENGTH,
    order_length=ORDER_NUMBER_LENGTH,
)

Session = Annotated[AsyncSession, Depends(get_session)]
FormInt = Annotated[int, Form()]
FormInts = Annotated[list[int], Form()]
RequestId = Annotated[UUID, Form()]


async def render(
    request: Request,
    url: str,
    template: str,
    title: str,
    error: Exception | None,
    **context: object,
) -> HTMLResponse:
    """Страница с итогом прошлого действия, после ошибки форма сохраняет введенные значения"""
    form = await request.form() if error else FormData()
    notice = request.cookies.get("notice")
    response = templates.TemplateResponse(
        request,
        template,
        {
            "url": url,
            "title": title,
            "notice": unquote(notice) if notice else None,
            "error": error,
            "split": error.split if isinstance(error, Shortage) else None,
            "form": form,
            # повторная отправка той же формы придет с тем же ключом и не проведет операцию
            "request_id": uuid4(),
            **context,
        },
        status_code=400 if error else 200,
    )
    if notice:
        response.delete_cookie("notice")
    # кнопка Назад загружает страницу заново с новым ключом формы, а не берет старую из кэша
    response.headers["Cache-Control"] = "no-store"
    return response


def redirect(url: str, notice: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.set_cookie("notice", quote(notice), httponly=True, samesite="lax")
    return response


def error_page(request: Request, message: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "error.html", {"title": "Ошибка", "error": message}, status_code=status_code
    )


async def invalid_form(request: Request, error: Exception) -> HTMLResponse:
    """Форма, которую браузер не пропустил бы, например буквы в количестве"""
    return error_page(request, "Форма заполнена неверно", 400)


async def page_not_found(request: Request, error: Exception) -> HTMLResponse:
    """Адрес, которого нет, или адрес кнопки, открытый как страница"""
    status_code = error.status_code if isinstance(error, HTTPException) else 404
    return error_page(request, "Страница не найдена", status_code)


async def choices(session: AsyncSession) -> dict[str, object]:
    products = await session.scalars(select(Product).order_by(Product.sku))
    active = await session.scalars(
        select(Warehouse).where(Warehouse.deleted_at.is_(None)).order_by(Warehouse.id)
    )
    return {
        "products": [(p.id, f"{p.sku} {p.name}") for p in products],
        "warehouses": [(w.id, w.name) for w in active],
    }


async def all_warehouses(session: AsyncSession) -> list[Warehouse]:
    return list(await session.scalars(select(Warehouse).order_by(Warehouse.id)))


@router.get("/")
async def stock_page(request: Request, session: Session) -> Response:
    """Остатки по каждому ФФ и товар вне ФФ, со сверкой по истории"""
    await inventory.use_snapshot(session)
    overview = await inventory.stock_overview(session)
    problems = await inventory.reconcile(session)
    return await render(
        request, "/", "stock.html", "Остатки", None, overview=overview, problems=problems
    )


async def show_receipts(
    request: Request, session: AsyncSession, error: InventoryError | None = None
) -> Response:
    await inventory.use_snapshot(session)
    return await render(
        request, "/receipts", "receipts.html", "Приход", error, **await choices(session)
    )


@router.get("/receipts")
async def receipts_page(request: Request, session: Session) -> Response:
    return await show_receipts(request, session)


@router.post("/receipts")
async def add_receipt(
    request: Request,
    session: Session,
    product_id: FormInt,
    warehouse_id: FormInt,
    qty: FormInt,
    request_id: RequestId,
) -> Response:
    """Приход товара на ФФ"""
    try:
        await inventory.receive(session, product_id, warehouse_id, qty, request_id)
    except AlreadyDone as done:
        return redirect("/", str(done))
    except InventoryError as error:
        return await show_receipts(request, session, error)
    return redirect("/", "Приход проведен")


async def show_transfers(
    request: Request, session: AsyncSession, error: InventoryError | None = None
) -> Response:
    await inventory.use_snapshot(session)
    warehouses = await all_warehouses(session)
    holding = set(await session.scalars(select(Stock.warehouse_id).where(Stock.on_hand > 0)))
    transfers = await session.execute(
        select(Transfer, Product)
        .join(Product, Transfer.product_id == Product.id)
        .where(Transfer.status == TransferStatus.IN_TRANSIT)
        .order_by(Transfer.id)
    )
    return await render(
        request,
        "/transfers",
        "transfers.html",
        "Перемещения между ФФ",
        error,
        # с удаленного ФФ товар можно только вывезти
        sources=[
            (w.id, ff_label(w)) for w in warehouses if w.deleted_at is None or w.id in holding
        ],
        labels={w.id: ff_label(w) for w in warehouses},
        transfers=transfers.all(),
        **await choices(session),
    )


@router.get("/transfers")
async def transfers_page(request: Request, session: Session) -> Response:
    return await show_transfers(request, session)


@router.post("/transfers")
async def send_transfer(
    request: Request,
    session: Session,
    product_id: FormInt,
    from_warehouse_id: FormInt,
    to_warehouse_id: FormInt,
    qty: FormInt,
    request_id: RequestId,
) -> Response:
    """Отправка товара на другой ФФ, до приемки он в пути"""
    try:
        transfer = await inventory.send_transfer(
            session, product_id, from_warehouse_id, to_warehouse_id, qty, request_id
        )
    except AlreadyDone as done:
        return redirect("/transfers", str(done))
    except InventoryError as error:
        return await show_transfers(request, session, error)
    return redirect("/transfers", f"Перемещение {transfer.id} отправлено")


@router.post("/transfers/{transfer_id}/receive")
async def receive_transfer(request: Request, session: Session, transfer_id: int) -> Response:
    """Приемка перемещения на ФФ назначения, повторная приемка ничего не меняет"""
    try:
        transfer = await inventory.receive_transfer(session, transfer_id)
    except AlreadyDone as done:
        return redirect("/transfers", str(done))
    except InventoryError as error:
        return await show_transfers(request, session, error)
    return redirect("/transfers", f"Перемещение {transfer.id} принято")


def allocations(warehouse_ids: list[int], quantities: list[int]) -> list[Allocation]:
    """ФФ и количества из формы, после нехватки их несколько"""
    if len(warehouse_ids) != len(quantities):
        raise InventoryError("Форма заполнена неверно")
    return [Allocation(id_, qty) for id_, qty in zip(warehouse_ids, quantities, strict=True)]


async def show_fbs(
    request: Request, session: AsyncSession, error: InventoryError | None = None
) -> Response:
    await inventory.use_snapshot(session)
    orders = await session.execute(
        select(FbsOrder, Product)
        .join(Product, FbsOrder.product_id == Product.id)
        .where(FbsOrder.status == FbsStatus.RESERVED)
        .order_by(FbsOrder.id)
    )
    return await render(
        request,
        "/fbs",
        "fbs.html",
        "FBS: резерв и отправка клиенту",
        error,
        labels={w.id: ff_label(w) for w in await all_warehouses(session)},
        orders=orders.all(),
        **await choices(session),
    )


@router.get("/fbs")
async def fbs_page(request: Request, session: Session) -> Response:
    return await show_fbs(request, session)


@router.post("/fbs")
async def reserve_fbs(
    request: Request,
    session: Session,
    product_id: FormInt,
    warehouse_id: FormInts,
    qty: FormInts,
    request_id: RequestId,
    order_number: Annotated[str, Form()] = "",
) -> Response:
    """Резерв под заказ FBS, после нехватки - сразу на нескольких ФФ"""
    try:
        orders = await inventory.reserve_fbs(
            session, order_number, product_id, allocations(warehouse_id, qty), request_id
        )
    except AlreadyDone as done:
        return redirect("/fbs", str(done))
    except InventoryError as error:
        return await show_fbs(request, session, error)
    return redirect("/fbs", f"Заказ {orders[0].order_number} зарезервирован")


@router.post("/fbs/{order_id}/ship")
async def ship_fbs(request: Request, session: Session, order_id: int) -> Response:
    """Отправка заказа FBS клиенту"""
    try:
        order = await inventory.ship_fbs(session, order_id)
    except AlreadyDone as done:
        return redirect("/fbs", str(done))
    except InventoryError as error:
        return await show_fbs(request, session, error)
    return redirect("/fbs", f"Заказ {order.order_number} отправлен клиенту")


@router.post("/fbs/{order_id}/cancel")
async def cancel_fbs(request: Request, session: Session, order_id: int) -> Response:
    """Отмена заказа FBS, товар снова доступен"""
    try:
        order = await inventory.cancel_fbs(session, order_id)
    except AlreadyDone as done:
        return redirect("/fbs", str(done))
    except InventoryError as error:
        return await show_fbs(request, session, error)
    return redirect("/fbs", f"Заказ {order.order_number} отменен, товар снова доступен")


async def show_fbo(
    request: Request, session: AsyncSession, error: InventoryError | None = None
) -> Response:
    await inventory.use_snapshot(session)
    shipments = await session.execute(
        select(FboShipment, Product)
        .join(Product, FboShipment.product_id == Product.id)
        .where(FboShipment.status == FboStatus.IN_TRANSIT)
        .order_by(FboShipment.id)
    )
    return await render(
        request,
        "/fbo",
        "fbo.html",
        "FBO: отгрузка на склад маркетплейса",
        error,
        labels={w.id: ff_label(w) for w in await all_warehouses(session)},
        shipments=shipments.all(),
        **await choices(session),
    )


@router.get("/fbo")
async def fbo_page(request: Request, session: Session) -> Response:
    return await show_fbo(request, session)


@router.post("/fbo")
async def send_fbo(
    request: Request,
    session: Session,
    product_id: FormInt,
    warehouse_id: FormInts,
    qty: FormInts,
    request_id: RequestId,
) -> Response:
    """Отгрузка на склад маркетплейса, после нехватки - сразу с нескольких ФФ"""
    try:
        shipments = await inventory.send_fbo(
            session, product_id, allocations(warehouse_id, qty), request_id
        )
    except AlreadyDone as done:
        return redirect("/fbo", str(done))
    except InventoryError as error:
        return await show_fbo(request, session, error)
    numbers = ", ".join(str(shipment.id) for shipment in shipments)
    return redirect("/fbo", f"Отгружено, поставка {numbers} в пути")


@router.post("/fbo/{shipment_id}/accept")
async def accept_fbo(request: Request, session: Session, shipment_id: int) -> Response:
    """Отметка, что маркетплейс принял поставку"""
    try:
        shipment = await inventory.accept_fbo(session, shipment_id)
    except AlreadyDone as done:
        return redirect("/fbo", str(done))
    except InventoryError as error:
        return await show_fbo(request, session, error)
    return redirect("/fbo", f"Поставка {shipment.id} принята маркетплейсом")


async def show_warehouses(
    request: Request, session: AsyncSession, error: InventoryError | None = None
) -> Response:
    await inventory.use_snapshot(session)
    holdings: dict[int, list[str]] = defaultdict(list)
    stock = await session.execute(
        select(Stock.warehouse_id, Product.sku, Stock.on_hand)
        .join(Product, Stock.product_id == Product.id)
        .where(Stock.on_hand > 0)
        .order_by(Product.sku)
    )
    for warehouse_id, sku, qty in stock:
        holdings[warehouse_id].append(f"{sku}: {qty}")
    warehouses = await all_warehouses(session)
    return await render(
        request,
        "/warehouses",
        "warehouses.html",
        "Склады фулфилмента",
        error,
        active=[w for w in warehouses if w.deleted_at is None],
        deleted=[(w, ", ".join(holdings[w.id])) for w in warehouses if w.deleted_at],
    )


@router.get("/warehouses")
async def warehouses_page(request: Request, session: Session) -> Response:
    return await show_warehouses(request, session)


@router.post("/warehouses")
async def add_warehouse(
    request: Request, session: Session, request_id: RequestId, name: Annotated[str, Form()] = ""
) -> Response:
    """Новый ФФ"""
    try:
        warehouse = await inventory.create_warehouse(session, name, request_id)
    except AlreadyDone as done:
        return redirect("/warehouses", str(done))
    except InventoryError as error:
        return await show_warehouses(request, session, error)
    return redirect("/warehouses", f"ФФ {warehouse.name} добавлен")


@router.post("/warehouses/{warehouse_id}/rename")
async def rename_warehouse(
    request: Request, session: Session, warehouse_id: int, name: Annotated[str, Form()] = ""
) -> Response:
    """Новое название ФФ, история показывает его под новым именем"""
    try:
        warehouse = await inventory.rename_warehouse(session, warehouse_id, name)
    except InventoryError as error:
        return await show_warehouses(request, session, error)
    return redirect("/warehouses", f"ФФ переименован в {warehouse.name}")


@router.post("/warehouses/{warehouse_id}/delete")
async def delete_warehouse(request: Request, session: Session, warehouse_id: int) -> Response:
    """Удаление ФФ, остатки и история на нем сохраняются"""
    try:
        warehouse = await inventory.delete_warehouse(session, warehouse_id)
    except AlreadyDone as done:
        return redirect("/warehouses", str(done))
    except InventoryError as error:
        return await show_warehouses(request, session, error)
    return redirect("/warehouses", f"ФФ {warehouse.name} удален")


@dataclass
class Entry:
    id: int
    time: str
    kind: str
    product: str
    qty: int
    source: str
    target: str
    document: str


def route(operation: Operation, labels: dict[int, str]) -> tuple[str, str]:
    """Откуда и куда ушел товар в операции"""
    source = labels[operation.from_warehouse_id] if operation.from_warehouse_id else ""
    target = labels[operation.to_warehouse_id] if operation.to_warehouse_id else ""
    match operation.kind:
        case OperationKind.RECEIPT:
            return "поставщик", target
        case OperationKind.TRANSFER_SENT:
            return source, f"в пути на {target}"
        case OperationKind.TRANSFER_RECEIVED:
            return "в пути", target
        case OperationKind.FBO_SENT:
            return source, "в пути на склад маркетплейса"
        case OperationKind.FBO_ACCEPTED:
            return "в пути", "склад маркетплейса"
        case OperationKind.FBS_RESERVED:
            return source, f"резерв, {source}"
        case OperationKind.FBS_SHIPPED:
            return f"резерв, {source}", "клиент"
        case OperationKind.FBS_CANCELLED:
            return f"резерв, {source}", source


def document(operation: Operation, order_number: str | None) -> str:
    if operation.transfer_id:
        return f"Перемещение {operation.transfer_id}"
    if operation.fbo_shipment_id:
        return f"Поставка {operation.fbo_shipment_id}"
    if order_number:
        return f"Заказ {order_number}"
    return ""


@router.get("/history")
async def history_page(request: Request, session: Session) -> Response:
    """Все операции, новые сверху"""
    await inventory.use_snapshot(session)
    labels = {w.id: ff_label(w) for w in await all_warehouses(session)}
    rows = await session.execute(
        select(Operation, Product, FbsOrder.order_number)
        .join(Product, Operation.product_id == Product.id)
        .outerjoin(FbsOrder, Operation.fbs_order_id == FbsOrder.id)
        .order_by(Operation.id.desc())
    )
    entries = [
        Entry(
            operation.id,
            when(operation.created_at),
            KINDS[operation.kind],
            f"{product.sku} {product.name}",
            operation.qty,
            *route(operation, labels),
            document(operation, order_number),
        )
        for operation, product, order_number in rows
    ]
    return await render(
        request, "/history", "history.html", "История операций", None, entries=entries
    )
