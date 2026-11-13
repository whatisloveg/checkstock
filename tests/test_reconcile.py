from sqlalchemy import text

from app import inventory


async def test_tz_data_reconciles(session, data):
    assert await inventory.reconcile(session) == []


async def test_stock_changed_outside_operations_is_found(session, data):
    await session.execute(text("UPDATE stock SET on_hand = 99 WHERE on_hand = 10"))
    await session.commit()

    problems = await inventory.reconcile(session)

    assert "SKU-101, ФФ Север: физически 99, по истории 10" in problems


async def test_reserve_changed_outside_operations_is_found(session, data):
    await session.execute(text("UPDATE stock SET reserved = 1 WHERE on_hand = 10"))
    await session.commit()

    problems = await inventory.reconcile(session)

    assert "SKU-101, ФФ Север: в резерве 1, по истории 0" in problems


async def test_goods_lost_in_transit_are_found(session, data):
    sku, north, south = data.products["SKU-101"], data.warehouses["Север"], data.warehouses["Юг"]
    await inventory.send_transfer(session, sku, north, south, 4)
    await session.execute(text("UPDATE transfers SET status = 'received'"))
    await session.commit()

    problems = await inventory.reconcile(session)

    assert "SKU-101: пришло 16, учтено 12" in problems


async def test_goods_out_of_nowhere_are_found(session, data):
    await session.execute(
        text(
            "INSERT INTO fbo_shipments (product_id, warehouse_id, qty, status)"
            " SELECT product_id, warehouse_id, 3, 'in_transit' FROM stock WHERE on_hand = 4"
        )
    )
    await session.commit()

    problems = await inventory.reconcile(session)

    assert "SKU-103: пришло 4, учтено 7" in problems


async def test_screen_is_read_from_one_snapshot(sessions, data):
    sku, north = data.products["SKU-101"], data.warehouses["Север"]
    async with sessions() as reader:
        await inventory.use_snapshot(reader)
        before = await inventory.stock_overview(reader)

        async with sessions() as writer:
            await inventory.receive(writer, sku, north, 5)

        after = await inventory.stock_overview(reader)
        assert after.products[0].cell(north) == before.products[0].cell(north)
        assert await inventory.reconcile(reader) == []
