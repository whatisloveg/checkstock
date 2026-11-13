import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models import Operation
from tests.page import find_all, find_form, rows, send, submit

VERSIONS = ["desktop", "mobile"]
PAGES = ["/", "/receipts", "/transfers", "/fbs", "/fbo", "/warehouses", "/history"]


@pytest.mark.parametrize("url", PAGES)
async def test_every_page_has_desktop_and_mobile_versions(client, data, url):
    page = await client.get(url)

    assert page.status_code == 200
    assert 'data-version="desktop"' in page.text
    assert 'data-version="mobile"' in page.text


async def test_stock_page_shows_every_product_on_every_warehouse(client, data):
    page = (await client.get("/")).text

    desktop = rows(page)
    assert desktop["SKU-101"] == ["SKU-101", "Органайзер", "10", "10", "0", "6", "6", "0"]
    assert desktop["SKU-102"] == ["SKU-102", "Контейнер", "5", "5", "0", "0", "0", "0"]
    assert desktop["SKU-103"] == ["SKU-103", "Корзина", "0", "0", "0", "4", "4", "0"]
    assert desktop["SKU-101 вне ФФ"] == ["SKU-101", "Органайзер", "0", "0", "0", "0"]
    mobile = rows(page, "mobile")
    assert mobile["SKU-103 Север"] == ["Север", "0", "0", "0"]
    assert mobile["SKU-103 Юг"] == ["Юг", "4", "4", "0"]
    assert "Остатки сходятся с историей операций" in page


async def test_stock_page_shows_goods_outside_warehouses(client, data):
    north, south = data.warehouses["Север"], data.warehouses["Юг"]
    sku = data.products["SKU-101"]
    await submit(
        client,
        "/transfers",
        "Отправить",
        product_id=sku,
        from_warehouse_id=north,
        to_warehouse_id=south,
        qty=4,
    )
    await submit(client, "/fbo", "Отгрузить", product_id=sku, warehouse_id=south, qty=1)
    await submit(
        client,
        "/fbs",
        "Зарезервировать",
        order_number="WB-1",
        product_id=sku,
        warehouse_id=north,
        qty=2,
    )
    await submit(client, "/fbs", "Отправить")

    page = (await client.get("/")).text

    assert rows(page)["SKU-101"] == ["SKU-101", "Органайзер", "4", "4", "0", "5", "5", "0"]
    assert rows(page)["SKU-101 вне ФФ"] == ["SKU-101", "Органайзер", "4", "1", "0", "2"]
    assert rows(page, "mobile")["SKU-101 FBS к клиенту"] == ["FBS к клиенту", "2"]


@pytest.mark.parametrize("version", VERSIONS)
async def test_receipt_adds_stock(client, data, read_state, version):
    response = await submit(
        client,
        "/receipts",
        "Оформить приход",
        version,
        product_id=data.products["SKU-102"],
        warehouse_id=data.warehouses["Юг"],
        qty=5,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "Приход проведен" in (await client.get("/")).text
    assert (await read_state())["SKU-102"]["Юг"] == (5, 0, 5)


async def test_message_after_action_is_shown_once(client, data):
    await submit(client, "/receipts", "Оформить приход", qty=1)

    assert "Приход проведен" in (await client.get("/")).text
    assert "Приход проведен" not in (await client.get("/")).text


async def test_form_sent_twice_is_applied_once(client, data, read_state):
    form = find_form((await client.get("/receipts")).text, "Оформить приход")

    await send(client, form, qty=5)
    repeat = await send(client, form, qty=5)

    assert repeat.status_code == 303
    assert "Эта операция уже проведена" in (await client.get("/")).text
    assert (await read_state())["SKU-101"]["Север"] == (15, 0, 15)


@pytest.mark.parametrize(
    ("page", "button", "values"),
    [
        ("/receipts", "Оформить приход", {}),
        ("/transfers", "Отправить", {"to_warehouse_id": "Юг"}),
        ("/fbs", "Зарезервировать", {"order_number": "WB-1"}),
        ("/fbo", "Отгрузить", {}),
    ],
)
async def test_every_form_sent_twice_makes_one_operation(
    client, data, sessions, read_state, page, button, values
):
    values = {name: data.warehouses.get(value, value) for name, value in values.items()}
    form = find_form((await client.get(page)).text, button)

    await send(client, form, qty=2, **values)
    repeat = await send(client, form, qty=2, **values)

    assert repeat.status_code == 303
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 4 + 1
    await read_state()


async def test_double_click_on_form_is_applied_once(client, data, read_state):
    form = find_form((await client.get("/receipts")).text, "Оформить приход")

    await asyncio.gather(send(client, form, qty=5), send(client, form, qty=5))

    assert (await read_state())["SKU-101"]["Север"] == (15, 0, 15)


async def test_error_keeps_entered_values(client, data):
    response = await submit(
        client,
        "/fbs",
        "Зарезервировать",
        order_number=" ",
        product_id=data.products["SKU-102"],
        qty=3,
    )

    assert response.status_code == 400
    assert "Укажите номер заказа" in response.text
    form = find_form(response.text, "Зарезервировать")
    assert form.value("product_id") == str(data.products["SKU-102"])
    assert form.value("qty") == "3"


@pytest.mark.parametrize("version", VERSIONS)
async def test_shortage_offers_split_that_reserves_on_both_warehouses(
    client, data, read_state, version
):
    response = await submit(
        client,
        "/fbs",
        "Зарезервировать",
        version,
        order_number="WB-7",
        product_id=data.products["SKU-101"],
        warehouse_id=data.warehouses["Север"],
        qty=12,
    )

    assert response.status_code == 400
    assert "На ФФ Север доступно 10 из 12, не хватает 2. На других ФФ: Юг: 6" in response.text
    split = find_form(response.text, "Разделить: Север 10 + Юг 2", version)
    assert (await send(client, split)).status_code == 303
    state = await read_state()
    assert state["SKU-101"]["Север"] == (10, 10, 0)
    assert state["SKU-101"]["Юг"] == (6, 2, 4)
    orders = rows((await client.get("/fbs")).text)
    assert [row[0] for row in orders.values()] == ["WB-7", "WB-7"]


async def test_split_button_does_not_reserve_twice(client, data, read_state):
    response = await submit(
        client,
        "/fbs",
        "Зарезервировать",
        order_number="WB-7",
        product_id=data.products["SKU-101"],
        warehouse_id=data.warehouses["Север"],
        qty=12,
    )
    split = find_form(response.text, "Разделить: Север 10 + Юг 2")

    await send(client, split)
    await send(client, split)

    assert (await read_state())["SKU-101"]["Юг"] == (6, 2, 4)


async def test_fbo_shortage_offers_split(client, data, read_state):
    response = await submit(
        client,
        "/fbo",
        "Отгрузить",
        product_id=data.products["SKU-101"],
        warehouse_id=data.warehouses["Юг"],
        qty=8,
    )

    split = find_form(response.text, "Разделить: Юг 6 + Север 2")
    assert (await send(client, split)).status_code == 303
    state = await read_state()
    assert state["SKU-101"]["FBO в пути"] == 8
    assert state["SKU-101"]["Юг"] == (0, 0, 0)


async def test_shortage_without_enough_goods_offers_no_split(client, data):
    response = await submit(
        client,
        "/fbs",
        "Зарезервировать",
        order_number="WB-7",
        product_id=data.products["SKU-101"],
        qty=17,
    )

    assert "не хватает 7" in response.text
    assert "Разделить" not in response.text


async def test_transfer_is_received_once(client, data, read_state):
    await submit(
        client,
        "/transfers",
        "Отправить",
        product_id=data.products["SKU-101"],
        from_warehouse_id=data.warehouses["Север"],
        to_warehouse_id=data.warehouses["Юг"],
        qty=4,
    )
    assert (await read_state())["SKU-101"]["в пути между ФФ"] == 4
    receive = find_form((await client.get("/transfers")).text, "Принять")

    await send(client, receive)
    repeat = await send(client, receive)

    assert repeat.status_code == 303
    assert "Перемещение уже принято" in (await client.get("/transfers")).text
    state = await read_state()
    assert state["SKU-101"]["Юг"] == (10, 0, 10)
    assert state["SKU-101"]["в пути между ФФ"] == 0


async def test_transfer_to_same_warehouse_is_refused(client, data):
    north = data.warehouses["Север"]

    response = await submit(
        client, "/transfers", "Отправить", from_warehouse_id=north, to_warehouse_id=north, qty=1
    )

    assert response.status_code == 400
    assert "Перемещать на тот же ФФ нельзя" in response.text


@pytest.mark.parametrize("version", VERSIONS)
async def test_order_is_shipped_from_page(client, data, read_state, version):
    await submit(client, "/fbs", "Зарезервировать", version, order_number="WB-1", qty=3)

    await submit(client, "/fbs", "Отправить", version)

    state = await read_state()
    assert state["SKU-101"]["Север"] == (7, 0, 7)
    assert state["SKU-101"]["FBS к клиенту"] == 3


async def test_stale_page_cannot_cancel_shipped_order(client, data, read_state):
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)
    page = (await client.get("/fbs")).text

    await send(client, find_form(page, "Отправить"))
    response = await send(client, find_form(page, "Отменить"))

    assert response.status_code == 400
    assert "Заказ WB-1 уже отправлен, отменить нельзя" in response.text
    assert (await read_state())["SKU-101"]["FBS к клиенту"] == 3


async def test_order_shipped_from_two_tabs_ships_once(client, data, read_state):
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)
    ship = find_form((await client.get("/fbs")).text, "Отправить")

    await send(client, ship)
    repeat = await send(client, ship)

    assert repeat.status_code == 303
    assert "Заказ WB-1 уже отправлен" in (await client.get("/fbs")).text
    assert (await read_state())["SKU-101"]["FBS к клиенту"] == 3


async def test_stale_page_cannot_ship_cancelled_order(client, data, read_state):
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)
    page = (await client.get("/fbs")).text

    await send(client, find_form(page, "Отменить"))
    await send(client, find_form(page, "Отменить"))
    response = await send(client, find_form(page, "Отправить"))

    assert response.status_code == 400
    assert "Заказ WB-1 отменен, отправить его нельзя" in response.text
    assert (await read_state())["SKU-101"]["Север"] == (10, 0, 10)


async def test_cancel_returns_goods(client, data, read_state):
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)

    await submit(client, "/fbs", "Отменить", "mobile")

    assert (await read_state())["SKU-101"]["Север"] == (10, 0, 10)
    assert "Заказ WB-1 отменен" in (await client.get("/fbs")).text


async def test_fbo_shipment_is_accepted_once(client, data, read_state):
    await submit(client, "/fbo", "Отгрузить", qty=5)
    assert (await read_state())["SKU-101"]["FBO в пути"] == 5
    accept = find_form((await client.get("/fbo")).text, "Отметить приемку")

    await send(client, accept)
    await send(client, accept)

    assert "Поставка уже принята маркетплейсом" in (await client.get("/fbo")).text
    state = await read_state()
    assert state["SKU-101"]["FBO в пути"] == 0
    assert state["SKU-101"]["FBO принято"] == 5


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("/transfers/999/receive", "Перемещение не найдено"),
        ("/fbs/999/ship", "Заказ не найден"),
        ("/fbs/999/cancel", "Заказ не найден"),
        ("/fbo/999/accept", "Поставка не найдена"),
        ("/warehouses/999/rename", "ФФ не найден"),
        ("/warehouses/999/delete", "ФФ не найден"),
    ],
)
async def test_missing_document_shows_error_page(client, data, url, message):
    response = await client.post(url, data={"name": "Восток"})

    assert response.status_code == 400
    assert message in response.text


async def test_receipt_of_nothing_is_refused_with_values_kept(client, data):
    response = await submit(
        client, "/receipts", "Оформить приход", warehouse_id=data.warehouses["Юг"], qty=0
    )

    assert response.status_code == 400
    assert "Количество должно быть больше нуля" in response.text
    assert find_form(response.text, "Оформить приход").value("warehouse_id") == str(
        data.warehouses["Юг"]
    )


async def test_split_form_with_missing_quantity_is_refused(client, data):
    response = await client.post(
        "/fbs",
        data={
            "order_number": "WB-1",
            "product_id": data.products["SKU-101"],
            "warehouse_id": list(data.warehouses.values()),
            "qty": [1],
            "request_id": str(uuid4()),
        },
    )

    assert response.status_code == 400
    assert "Форма заполнена неверно" in response.text


async def test_broken_form_shows_page_instead_of_json(client, data):
    response = await client.post("/receipts", data={"qty": "много"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Форма заполнена неверно" in response.text


@pytest.mark.parametrize("version", VERSIONS)
async def test_warehouse_is_added_renamed_and_deleted(client, data, version):
    await submit(client, "/warehouses", "Добавить", version, name="Восток")
    page = (await client.get("/warehouses")).text
    [rename] = [f for f in find_all(page, "Переименовать", version) if f.value("name") == "Восток"]

    await send(client, rename, name="Запад")
    page = (await client.get("/warehouses")).text
    delete = find_form(page, "Удалить", version, action=rename.action.replace("rename", "delete"))
    await send(client, delete)

    page = (await client.get("/warehouses")).text
    assert "ФФ Запад удален" in page
    assert rows(page, version)["Запад"][0] == "Запад"
    assert [f.value("name") for f in find_all(page, "Переименовать", version)] == ["Север", "Юг"]
    transfer = find_form((await client.get("/transfers")).text, "Отправить", version)
    assert "Запад (удален)" not in transfer.options["from_warehouse_id"]


async def test_stale_page_cannot_rename_deleted_warehouse(client, data):
    south = data.warehouses["Юг"]
    page = (await client.get("/warehouses")).text
    delete = find_form(page, "Удалить", action=f"/warehouses/{south}/delete")

    await send(client, delete)
    rename = await send(client, find_form(page, "Переименовать", action=f"/{south}/rename"))
    repeat = await send(client, delete)

    assert rename.status_code == 400
    assert "ФФ Юг удален, переименовать нельзя" in rename.text
    assert repeat.status_code == 303
    assert "ФФ Юг уже удален" in (await client.get("/warehouses")).text


async def test_warehouse_name_must_be_unique(client, data):
    response = await submit(client, "/warehouses", "Добавить", name="север")

    assert response.status_code == 400
    assert "ФФ с названием север уже есть" in response.text


async def test_deleted_warehouse_keeps_stock_and_is_offered_only_as_source(client, data):
    south = data.warehouses["Юг"]

    await submit(client, "/warehouses", "Удалить", action=f"/warehouses/{south}/delete")

    stock = (await client.get("/")).text
    assert "Юг (удален)" in stock
    assert rows(stock)["SKU-101"][5:] == ["6", "6", "0"]
    assert rows((await client.get("/warehouses")).text)["Юг"] == ["Юг", "SKU-101: 6, SKU-103: 4"]
    transfer = find_form((await client.get("/transfers")).text, "Отправить")
    assert transfer.options["from_warehouse_id"] == ["Север", "Юг (удален)"]
    assert transfer.options["to_warehouse_id"] == ["Север"]
    receipt = find_form((await client.get("/receipts")).text, "Оформить приход")
    assert receipt.options["warehouse_id"] == ["Север"]


async def test_history_shows_what_how_many_from_and_to(client, data, sessions):
    await submit(
        client,
        "/transfers",
        "Отправить",
        product_id=data.products["SKU-101"],
        from_warehouse_id=data.warehouses["Север"],
        to_warehouse_id=data.warehouses["Юг"],
        qty=4,
    )
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=2)
    await submit(client, "/fbs", "Отправить")
    async with sessions() as session:
        ids = list(await session.scalars(select(Operation.id).order_by(Operation.id)))

    history = rows((await client.get("/history")).text)

    receipt, *_, sent, reserved, shipped = (history[str(id_)][1:] for id_ in ids)
    assert receipt == ["Приход", "SKU-101 Органайзер", "10", "поставщик", "Север", ""]
    assert sent == [
        "Перемещение: отправка",
        "SKU-101 Органайзер",
        "4",
        "Север",
        "в пути на Юг",
        "Перемещение 1",
    ]
    assert reserved == [
        "FBS: резерв",
        "SKU-101 Органайзер",
        "2",
        "Север",
        "резерв, Север",
        "Заказ WB-1",
    ]
    assert shipped == [
        "FBS: отправка клиенту",
        "SKU-101 Органайзер",
        "2",
        "резерв, Север",
        "клиент",
        "Заказ WB-1",
    ]
    assert list(history) == [str(id_) for id_ in reversed(ids)]


async def test_history_names_every_kind_of_operation(client, data):
    north, south = data.warehouses["Север"], data.warehouses["Юг"]
    await submit(
        client, "/transfers", "Отправить", from_warehouse_id=north, to_warehouse_id=south, qty=1
    )
    await submit(client, "/transfers", "Принять")
    await submit(client, "/fbo", "Отгрузить", qty=1)
    await submit(client, "/fbo", "Отметить приемку")
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-2", qty=1)
    await submit(client, "/fbs", "Отменить")

    history = rows((await client.get("/history")).text)

    product = "SKU-101 Органайзер"
    assert [row[1:] for row in list(history.values())[:6]] == [
        ["FBS: отмена резерва", product, "1", "резерв, Север", "Север", "Заказ WB-2"],
        ["FBS: резерв", product, "1", "Север", "резерв, Север", "Заказ WB-2"],
        ["FBO: приемка маркетплейсом", product, "1", "в пути", "склад маркетплейса", "Поставка 1"],
        ["FBO: отгрузка", product, "1", "Север", "в пути на склад маркетплейса", "Поставка 1"],
        ["Перемещение: приемка", product, "1", "в пути", "Юг", "Перемещение 1"],
        ["Перемещение: отправка", product, "1", "Север", "в пути на Юг", "Перемещение 1"],
    ]


async def test_history_marks_deleted_warehouse(client, data):
    south = data.warehouses["Юг"]
    await submit(client, "/warehouses", "Удалить", action=f"/warehouses/{south}/delete")
    await submit(client, "/warehouses", "Добавить", name="Юг")

    history = rows((await client.get("/history")).text)

    assert history["2"][5] == "Юг (удален)"


@pytest.mark.parametrize(
    ("page", "button"),
    [
        ("/receipts", "Оформить приход"),
        ("/transfers", "Отправить"),
        ("/fbs", "Зарезервировать"),
        ("/fbo", "Отгрузить"),
        ("/warehouses", "Добавить"),
    ],
)
async def test_back_button_brings_fresh_empty_form(client, data, page, button):
    response = await client.get(page)

    assert response.headers["cache-control"] == "no-store"
    for version in VERSIONS:
        assert find_form(response.text, button, version).autocomplete == "off"


async def test_old_form_sent_with_other_values_is_new_receipt(client, data, read_state):
    form = find_form((await client.get("/receipts")).text, "Оформить приход")

    await send(client, form, qty=5)
    await send(client, form, qty=2)

    assert (await read_state())["SKU-101"]["Север"] == (17, 0, 17)


async def test_huge_quantity_is_refused_without_crash(client, data):
    response = await submit(client, "/receipts", "Оформить приход", qty=3_000_000_000)

    assert response.status_code == 400
    assert "Количество больше 1000000, проверьте ввод" in response.text


async def test_double_click_on_add_warehouse_shows_no_error(client, data):
    form = find_form((await client.get("/warehouses")).text, "Добавить")

    await send(client, form, name="Восток")
    repeat = await send(client, form, name="Восток")

    assert repeat.status_code == 303
    page = (await client.get("/warehouses")).text
    assert [f.value("name") for f in find_all(page, "Переименовать")] == ["Север", "Юг", "Восток"]


async def test_warehouse_forms_keep_entered_name_after_error(client, data):
    north = data.warehouses["Север"]

    added = await submit(client, "/warehouses", "Добавить", name="Юг")
    renamed = await submit(
        client, "/warehouses", "Переименовать", action=f"/{north}/rename", name="юг"
    )

    assert find_form(added.text, "Добавить").value("name") == "Юг"
    assert renamed.status_code == 400
    assert find_form(renamed.text, "Добавить").value("name") == ""
    assert [f.value("name") for f in find_all(renamed.text, "Переименовать")] == ["юг", "Юг"]


@pytest.mark.parametrize("url", ["/nowhere", "/fbs/1/ship"])
async def test_unknown_address_shows_page_instead_of_json(client, data, url):
    response = await client.get(url)

    assert response.status_code in (404, 405)
    assert response.headers["content-type"].startswith("text/html")
    assert "Страница не найдена" in response.text


async def test_transfer_form_suggests_another_warehouse(client, data):
    form = find_form((await client.get("/transfers")).text, "Отправить")

    assert form.value("from_warehouse_id") != form.value("to_warehouse_id")


async def test_offer_from_one_warehouse_is_not_called_split(client, data, read_state):
    response = await submit(
        client,
        "/fbs",
        "Зарезервировать",
        order_number="WB-9",
        product_id=data.products["SKU-103"],
        qty=3,
    )

    await send(client, find_form(response.text, "Взять с другого ФФ: Юг 3"))

    assert (await read_state())["SKU-103"]["Юг"] == (4, 3, 1)


async def test_same_order_entered_again_is_refused(client, data, read_state):
    await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)

    response = await submit(client, "/fbs", "Зарезервировать", order_number="WB-1", qty=3)

    assert response.status_code == 400
    assert "Заказ WB-1 по SKU-101 уже зарезервирован" in response.text
    assert (await read_state())["SKU-101"]["Север"] == (10, 3, 7)
