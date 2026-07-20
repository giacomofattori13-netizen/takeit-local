from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError


@dataclass(frozen=True)
class ColumnMigration:
    table_name: str
    column_name: str
    ddl: str


STARTUP_COLUMN_MIGRATIONS: tuple[ColumnMigration, ...] = (
    ColumnMigration(
        "conversationsession",
        "customer_phone",
        "ALTER TABLE conversationsession ADD COLUMN customer_phone VARCHAR",
    ),
    ColumnMigration(
        "conversationsession",
        "intended_quantity",
        "ALTER TABLE conversationsession ADD COLUMN intended_quantity INTEGER",
    ),
    ColumnMigration(
        "conversationsession",
        "pending_customer_name",
        "ALTER TABLE conversationsession ADD COLUMN pending_customer_name VARCHAR",
    ),
    ColumnMigration(
        "conversationsession",
        "favorite_pizzas_json",
        "ALTER TABLE conversationsession ADD COLUMN favorite_pizzas_json VARCHAR DEFAULT '[]'",
    ),
    ColumnMigration(
        "conversationsession",
        "no_input_count",
        "ALTER TABLE conversationsession ADD COLUMN no_input_count INTEGER DEFAULT 0",
    ),
    ColumnMigration(
        "conversationsession",
        "reservation_json",
        "ALTER TABLE conversationsession ADD COLUMN reservation_json VARCHAR DEFAULT '{}'",
    ),
    ColumnMigration(
        "order",
        "conversation_session_id",
        'ALTER TABLE "order" ADD COLUMN conversation_session_id VARCHAR',
    ),
    ColumnMigration(
        "orderitem",
        "dough_type",
        "ALTER TABLE orderitem ADD COLUMN dough_type VARCHAR DEFAULT 'classica'",
    ),
    ColumnMigration(
        "orderitem",
        "size",
        "ALTER TABLE orderitem ADD COLUMN size VARCHAR DEFAULT 'normale'",
    ),
    ColumnMigration(
        "menuitem",
        "sale_unit",
        "ALTER TABLE menuitem ADD COLUMN sale_unit VARCHAR DEFAULT 'piece'",
    ),
    ColumnMigration(
        "orderitem",
        "sale_unit",
        "ALTER TABLE orderitem ADD COLUMN sale_unit VARCHAR DEFAULT 'piece'",
    ),
    ColumnMigration(
        "conversationsession",
        "restaurant_id",
        "ALTER TABLE conversationsession ADD COLUMN restaurant_id VARCHAR",
    ),
    ColumnMigration(
        "menuitem",
        "restaurant_id",
        "ALTER TABLE menuitem ADD COLUMN restaurant_id VARCHAR",
    ),
    ColumnMigration(
        "orderitem",
        "temperature",
        "ALTER TABLE orderitem ADD COLUMN temperature VARCHAR",
    ),
    ColumnMigration(
        "conversationsession",
        "kg_temperature",
        "ALTER TABLE conversationsession ADD COLUMN kg_temperature VARCHAR",
    ),
    ColumnMigration(
        "order",
        "pickup_date",
        'ALTER TABLE "order" ADD COLUMN pickup_date VARCHAR',
    ),
    ColumnMigration(
        "orderitem",
        "portion",
        "ALTER TABLE orderitem ADD COLUMN portion VARCHAR",
    ),
)

ORDER_IDEMPOTENCY_INDEX_NAME = "ix_order_conversation_session_id_unique"
ORDER_IDEMPOTENCY_INDEX_DDL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {ORDER_IDEMPOTENCY_INDEX_NAME} "
    'ON "order" (conversation_session_id) '
    "WHERE conversation_session_id IS NOT NULL"
)


def apply_startup_column_migrations(
    db_engine: Engine,
    migrations: Sequence[ColumnMigration] = STARTUP_COLUMN_MIGRATIONS,
) -> list[str]:
    """Apply legacy SQLite column migrations only when the target column is absent."""
    applied: list[str] = []
    columns_by_table: dict[str, set[str]] = {}

    with db_engine.begin() as conn:
        inspector = inspect(conn)
        table_names = set(inspector.get_table_names())

        for migration in migrations:
            if migration.table_name not in table_names:
                raise RuntimeError(
                    "Startup migration target table missing: "
                    f"{migration.table_name}.{migration.column_name}"
                )

            columns = columns_by_table.get(migration.table_name)
            if columns is None:
                columns = {
                    column["name"]
                    for column in inspector.get_columns(migration.table_name)
                }
                columns_by_table[migration.table_name] = columns

            if migration.column_name in columns:
                continue

            try:
                conn.execute(text(migration.ddl))
            except SQLAlchemyError as exc:
                raise RuntimeError(
                    "Startup migration failed: "
                    f"{migration.table_name}.{migration.column_name}"
                ) from exc

            columns.add(migration.column_name)
            applied.append(f"{migration.table_name}.{migration.column_name}")

    return applied


def ensure_order_idempotency_index(db_engine: Engine) -> bool:
    """Ensure the unique partial index used to make order confirmation idempotent."""
    with db_engine.begin() as conn:
        inspector = inspect(conn)
        table_names = set(inspector.get_table_names())
        if "order" not in table_names:
            raise RuntimeError("Startup index target table missing: order")

        existing_indexes = {
            index["name"]
            for index in inspector.get_indexes("order")
        }
        if ORDER_IDEMPOTENCY_INDEX_NAME in existing_indexes:
            return False

        try:
            conn.execute(text(ORDER_IDEMPOTENCY_INDEX_DDL))
        except SQLAlchemyError as exc:
            raise RuntimeError("Startup index failed: order.conversation_session_id") from exc

    return True
