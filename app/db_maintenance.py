from sqlalchemy import text

from app.database import engine


def ensure_order_sync_schema() -> None:
    """
    Lightweight SQLite schema migration for existing local/VPS databases.
    SQLAlchemy create_all creates new tables, but it does not add new columns
    to existing tables. This safely adds missing columns.
    """

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(order_sync)")).mappings().all()

        if not rows:
            return

        existing_columns = {row["name"] for row in rows}

        columns_to_add = {
            "shopify_order_id": "shopify_order_id VARCHAR(100)",
            "shopify_order_name": "shopify_order_name VARCHAR(100)",
            "shopify_order_admin_url": "shopify_order_admin_url VARCHAR(255)",
        }

        for column_name, column_sql in columns_to_add.items():
            if column_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE order_sync ADD COLUMN {column_sql}"))
