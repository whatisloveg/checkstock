from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# постоянные имена ограничений нужны, чтобы миграции не плодили случайные
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# длины строк в схеме, учет проверяет их до записи
WAREHOUSE_NAME_LENGTH = 100
ORDER_NUMBER_LENGTH = 64


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def status_enum(enum: type[StrEnum], name: str) -> Enum:
    # varchar с CHECK проще менять миграцией, чем enum Postgres
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda members: [member.value for member in members],
    )


class Product(Base):
    """Товар продавца"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class Warehouse(Base):
    """Склад фулфилмента, после удаления остается в истории"""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(WAREHOUSE_NAME_LENGTH))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# удаленный ФФ не мешает завести новый с тем же названием
Index(
    "uq_warehouses_active_name",
    func.lower(Warehouse.name),
    unique=True,
    postgresql_where=Warehouse.deleted_at.is_(None),
)


class Stock(Base):
    """Остаток товара на ФФ, доступно = физически - резерв"""

    __tablename__ = "stock"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="on_hand_not_negative"),
        CheckConstraint("reserved >= 0", name="reserved_not_negative"),
        CheckConstraint("reserved <= on_hand", name="reserved_within_on_hand"),
    )

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(default=0, server_default="0")

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class TransferStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"


class Transfer(Base):
    """Перемещение товара между ФФ"""

    __tablename__ = "transfers"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("from_warehouse_id <> to_warehouse_id", name="different_warehouses"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    qty: Mapped[int]
    status: Mapped[TransferStatus] = mapped_column(
        status_enum(TransferStatus, "transfer_status"), default=TransferStatus.IN_TRANSIT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FboStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    ACCEPTED = "accepted"


class FboShipment(Base):
    """Поставка на склад маркетплейса по FBO"""

    __tablename__ = "fbo_shipments"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    qty: Mapped[int]
    status: Mapped[FboStatus] = mapped_column(
        status_enum(FboStatus, "fbo_status"), default=FboStatus.IN_TRANSIT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FbsStatus(StrEnum):
    RESERVED = "reserved"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


class FbsOrder(Base):
    """Заказ FBS, держит резерв на ФФ до отправки или отмены"""

    __tablename__ = "fbs_orders"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(ORDER_NUMBER_LENGTH))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    qty: Mapped[int]
    status: Mapped[FbsStatus] = mapped_column(
        status_enum(FbsStatus, "fbs_status"), default=FbsStatus.RESERVED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationKind(StrEnum):
    RECEIPT = "receipt"
    TRANSFER_SENT = "transfer_sent"
    TRANSFER_RECEIVED = "transfer_received"
    FBO_SENT = "fbo_sent"
    FBO_ACCEPTED = "fbo_accepted"
    FBS_RESERVED = "fbs_reserved"
    FBS_SHIPPED = "fbs_shipped"
    FBS_CANCELLED = "fbs_cancelled"


class Operation(Base):
    """Запись истории: что, сколько, откуда и куда, только добавляется"""

    __tablename__ = "operations"
    __table_args__ = (CheckConstraint("qty > 0", name="qty_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[OperationKind] = mapped_column(status_enum(OperationKind, "operation_kind"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    transfer_id: Mapped[int | None] = mapped_column(ForeignKey("transfers.id"))
    fbo_shipment_id: Mapped[int | None] = mapped_column(ForeignKey("fbo_shipments.id"))
    fbs_order_id: Mapped[int | None] = mapped_column(ForeignKey("fbs_orders.id"))


class ProcessedRequest(Base):
    """Ключ уже выполненной формы, повторная отправка не проводит операцию второй раз"""

    __tablename__ = "processed_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
