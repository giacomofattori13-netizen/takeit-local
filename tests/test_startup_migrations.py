import unittest

from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import app.models  # noqa: F401
from app.startup_migrations import (
    ColumnMigration,
    ORDER_IDEMPOTENCY_INDEX_NAME,
    apply_startup_column_migrations,
    ensure_order_idempotency_index,
)


def make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class StartupMigrationTests(unittest.TestCase):
    def test_current_schema_needs_no_legacy_column_migrations(self):
        engine = make_engine()
        SQLModel.metadata.create_all(engine)

        applied = apply_startup_column_migrations(engine)

        self.assertEqual(applied, [])

    def test_applies_only_missing_columns_and_is_idempotent(self):
        engine = make_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE conversationsession ("
                    "id INTEGER PRIMARY KEY, "
                    "session_id VARCHAR, "
                    "customer_phone VARCHAR"
                    ")"
                )
            )

        migrations = [
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
        ]

        first_run = apply_startup_column_migrations(engine, migrations)
        second_run = apply_startup_column_migrations(engine, migrations)

        columns = {
            column["name"]
            for column in inspect(engine).get_columns("conversationsession")
        }
        self.assertEqual(first_run, ["conversationsession.intended_quantity"])
        self.assertEqual(second_run, [])
        self.assertIn("customer_phone", columns)
        self.assertIn("intended_quantity", columns)

    def test_missing_table_is_not_silently_ignored(self):
        engine = make_engine()
        migrations = [
            ColumnMigration(
                "conversationsession",
                "customer_phone",
                "ALTER TABLE conversationsession ADD COLUMN customer_phone VARCHAR",
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "target table missing"):
            apply_startup_column_migrations(engine, migrations)

    def test_bad_ddl_is_not_silently_ignored(self):
        engine = make_engine()
        with engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE conversationsession (id INTEGER PRIMARY KEY)")
            )

        migrations = [
            ColumnMigration(
                "conversationsession",
                "customer_phone",
                "ALTER TABLE conversationsession ADD COLUMN",
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "Startup migration failed"):
            apply_startup_column_migrations(engine, migrations)

    def test_order_idempotency_index_is_created_once(self):
        engine = make_engine()
        SQLModel.metadata.create_all(engine)

        first_run = ensure_order_idempotency_index(engine)
        second_run = ensure_order_idempotency_index(engine)

        index_names = {
            index["name"]
            for index in inspect(engine).get_indexes("order")
        }
        self.assertTrue(first_run)
        self.assertFalse(second_run)
        self.assertIn(ORDER_IDEMPOTENCY_INDEX_NAME, index_names)

    def test_order_idempotency_index_missing_table_is_not_ignored(self):
        engine = make_engine()

        with self.assertRaisesRegex(RuntimeError, "target table missing"):
            ensure_order_idempotency_index(engine)


if __name__ == "__main__":
    unittest.main()
