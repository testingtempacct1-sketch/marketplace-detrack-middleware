from sqlalchemy import text

from app.database import engine


def ensure_order_sync_schema() -> None:
    """
    Lightweight SQLite schema migration for existing local/VPS databases.
    SQLAlchemy create_all creates new tables, but it does not add new columns
    to existing tables. This safely adds missing columns and tables.
    """

    with engine.begin() as conn:
        # ── order_sync columns ──────────────────────────────────────────
        rows = conn.execute(text("PRAGMA table_info(order_sync)")).mappings().all()

        if rows:
            existing_columns = {row["name"] for row in rows}

            columns_to_add = {
                "shopify_order_id": "shopify_order_id VARCHAR(100)",
                "shopify_order_name": "shopify_order_name VARCHAR(100)",
                "shopify_order_admin_url": "shopify_order_admin_url VARCHAR(255)",
                "retry_count": "retry_count INTEGER NOT NULL DEFAULT 0",
                "last_retry_at": "last_retry_at DATETIME",
                "next_retry_at": "next_retry_at DATETIME",
            }

            for column_name, column_sql in columns_to_add.items():
                if column_name not in existing_columns:
                    conn.execute(
                        text(f"ALTER TABLE order_sync ADD COLUMN {column_sql}")
                    )

        # ── order_sync_log table ────────────────────────────────────────
        log_rows = conn.execute(
            text("PRAGMA table_info(order_sync_log)")
        ).mappings().all()

        if not log_rows:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS order_sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_sync_id INTEGER NOT NULL REFERENCES order_sync(id) ON DELETE CASCADE,
                    log_type VARCHAR(20) NOT NULL,
                    from_status VARCHAR(100),
                    to_status VARCHAR(100),
                    note TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_order_sync_log_order_sync_id "
                "ON order_sync_log (order_sync_id)"
            ))
