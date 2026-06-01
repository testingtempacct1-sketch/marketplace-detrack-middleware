import logging

from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)


def ensure_order_sync_schema() -> None:
    """
    Schema migration that works with both SQLite and PostgreSQL.
    SQLAlchemy create_all handles new tables.
    This function safely adds missing columns to existing tables.
    """

    dialect = engine.dialect.name

    with engine.begin() as conn:
        if dialect == "sqlite":
            _migrate_sqlite(conn)
        elif dialect == "postgresql":
            _migrate_postgresql(conn)
        else:
            logger.warning(f"[DB Maintenance] Unknown dialect: {dialect}. Skipping migration.")


def _migrate_sqlite(conn) -> None:
    """SQLite-specific migration using PRAGMA."""
    rows = conn.execute(text("PRAGMA table_info(order_sync)")).mappings().all()

    if not rows:
        return

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
            conn.execute(text(f"ALTER TABLE order_sync ADD COLUMN {column_sql}"))
            logger.info(f"[DB Maintenance] Added column: {column_name}")

    # Create order_sync_log table if missing
    log_rows = conn.execute(text("PRAGMA table_info(order_sync_log)")).mappings().all()
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
        logger.info("[DB Maintenance] Created order_sync_log table (SQLite).")


def _migrate_postgresql(conn) -> None:
    """PostgreSQL-specific migration using information_schema."""

    # Check existing columns in order_sync
    result = conn.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'order_sync'
        AND table_schema = 'public'
    """)).fetchall()

    existing_columns = {row[0] for row in result}

    if not existing_columns:
        # Table doesn't exist yet — create_all will handle it
        logger.info("[DB Maintenance] order_sync table not found. Will be created by SQLAlchemy.")
        return

    columns_to_add = {
        "shopify_order_id": "VARCHAR(100)",
        "shopify_order_name": "VARCHAR(100)",
        "shopify_order_admin_url": "VARCHAR(255)",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "last_retry_at": "TIMESTAMP",
        "next_retry_at": "TIMESTAMP",
    }

    for column_name, column_type in columns_to_add.items():
        if column_name not in existing_columns:
            conn.execute(text(
                f"ALTER TABLE order_sync ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
            ))
            logger.info(f"[DB Maintenance] Added column: {column_name}")

    # Check if order_sync_log table exists
    log_result = conn.execute(text("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_name = 'order_sync_log'
        AND table_schema = 'public'
    """)).fetchone()

    if not log_result:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS order_sync_log (
                id SERIAL PRIMARY KEY,
                order_sync_id INTEGER NOT NULL REFERENCES order_sync(id) ON DELETE CASCADE,
                log_type VARCHAR(20) NOT NULL,
                from_status VARCHAR(100),
                to_status VARCHAR(100),
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_order_sync_log_order_sync_id "
            "ON order_sync_log (order_sync_id)"
        ))
        logger.info("[DB Maintenance] Created order_sync_log table (PostgreSQL).")
