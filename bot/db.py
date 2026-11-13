"""SQLite persistence for users, busy lock, and diamond ledger."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from bot.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class User:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    diamonds: int  # computed from ledger SUM(delta), not stored on users
    is_busy: bool
    created_at: str
    updated_at: str


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_path() -> Path:
    return Path(settings.sqlite_path).expanduser()


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def _balance_from_ledger(db: aiosqlite.Connection, telegram_id: int) -> int:
    async with db.execute(
        "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE telegram_id = ?",
        (telegram_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def _write_ledger(
    db: aiosqlite.Connection,
    telegram_id: int,
    delta: int,
    reason: str,
) -> None:
    await db.execute(
        """
        INSERT INTO ledger (telegram_id, delta, reason, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (telegram_id, delta, reason, _now()),
    )


async def init_db() -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_busy INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(telegram_id)"
        )

        # Make legacy non-unique reasons unique per user before UNIQUE(reason)
        async with db.execute(
            """
            SELECT id, telegram_id, reason FROM ledger
            WHERE reason IN ('signup_bonus', 'opening_balance')
            """
        ) as cursor:
            legacy_rows = await cursor.fetchall()
        for row_id, telegram_id, reason in legacy_rows:
            await db.execute(
                "UPDATE ledger SET reason = ? WHERE id = ?",
                (f"{reason}:{telegram_id}", row_id),
            )

        # One-time backfill: old users.diamonds cache → opening ledger row
        cols = await _table_columns(db, "users")
        if "diamonds" in cols:
            async with db.execute(
                "SELECT telegram_id, diamonds FROM users"
            ) as cursor:
                rows = await cursor.fetchall()
            for telegram_id, cached in rows:
                async with db.execute(
                    "SELECT COUNT(*) FROM ledger WHERE telegram_id = ?",
                    (telegram_id,),
                ) as count_cur:
                    count = int((await count_cur.fetchone())[0])
                if count == 0 and cached is not None and int(cached) != 0:
                    await _write_ledger(
                        db,
                        int(telegram_id),
                        int(cached),
                        f"opening_balance:{int(telegram_id)}",
                    )
                    logger.info(
                        "Backfilled ledger for user %s from cached diamonds=%s",
                        telegram_id,
                        cached,
                    )

        # Unique only for idempotent credit/refund reasons (legacy job:* may duplicate)
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_reason_unique
            ON ledger(reason)
            WHERE reason LIKE 'purchase:%'
               OR reason LIKE 'signup_bonus:%'
               OR reason LIKE 'opening_balance:%'
               OR reason LIKE 'refund:%'
            """
        )

        await db.commit()
    logger.info("SQLite ready at %s (balance = SUM(ledger.delta))", path)


def _row_to_user(row: aiosqlite.Row, *, diamonds: int) -> User:
    return User(
        telegram_id=int(row["telegram_id"]),
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        diamonds=int(diamonds),
        is_busy=bool(row["is_busy"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


async def upsert_user(
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    now = _now()
    starter = int(settings.starter_diamonds)
    path = _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT telegram_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            exists = await cursor.fetchone()

        if exists is None:
            await db.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, last_name,
                    is_busy, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, now, now),
            )
            await _write_ledger(
                db, telegram_id, starter, f"signup_bonus:{telegram_id}"
            )
        else:
            await db.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, now, telegram_id),
            )
        await db.commit()
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
        balance = await _balance_from_ledger(db, telegram_id)
    assert row is not None
    return _row_to_user(row, diamonds=balance)


async def get_user(telegram_id: int) -> User | None:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        balance = await _balance_from_ledger(db, telegram_id)
    return _row_to_user(row, diamonds=balance)


async def get_balance(telegram_id: int) -> int:
    async with aiosqlite.connect(_db_path()) as db:
        return await _balance_from_ledger(db, telegram_id)


async def try_set_busy(telegram_id: int) -> bool:
    """Atomically mark user busy. Returns False if already busy or missing."""
    now = _now()
    async with aiosqlite.connect(_db_path()) as db:
        cursor = await db.execute(
            """
            UPDATE users
            SET is_busy = 1, updated_at = ?
            WHERE telegram_id = ? AND is_busy = 0
            """,
            (now, telegram_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def clear_busy(telegram_id: int) -> None:
    now = _now()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            UPDATE users
            SET is_busy = 0, updated_at = ?
            WHERE telegram_id = ?
            """,
            (now, telegram_id),
        )
        await db.commit()


async def charge_diamonds(
    telegram_id: int,
    amount: int,
    *,
    reason: str = "job",
) -> bool:
    """Append a negative ledger entry if balance (SUM deltas) is enough."""
    if amount <= 0:
        return True
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        balance = await _balance_from_ledger(db, telegram_id)
        if balance < amount:
            await db.rollback()
            return False
        await _write_ledger(db, telegram_id, -amount, reason)
        await db.execute(
            "UPDATE users SET updated_at = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await db.commit()
        return True


async def add_diamonds(
    telegram_id: int,
    amount: int,
    *,
    reason: str = "credit",
) -> int:
    """Append a positive ledger entry and return new balance."""
    if amount == 0:
        return await get_balance(telegram_id)
    async with aiosqlite.connect(_db_path()) as db:
        await _write_ledger(db, telegram_id, amount, reason)
        await db.execute(
            "UPDATE users SET updated_at = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await db.commit()
        return await _balance_from_ledger(db, telegram_id)


async def ledger_has_reason(reason: str) -> bool:
    """True if any ledger row already has this exact reason (payment idempotency)."""
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT 1 FROM ledger WHERE reason = ? LIMIT 1",
            (reason,),
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None


async def credit_purchase_once(
    telegram_id: int,
    diamonds: int,
    *,
    provider_charge_id: str,
    package_id: str,
) -> tuple[bool, int]:
    """Credit diamonds for a Telegram/YooKassa payment at most once.

    Returns (credited_now, balance_after).
    """
    charge_id = (provider_charge_id or "").strip()
    if not charge_id:
        raise ValueError("provider_charge_id is required for payment idempotency")
    reason = f"purchase:{package_id}:{charge_id}"
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT 1 FROM ledger WHERE reason = ? LIMIT 1",
            (reason,),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists is not None:
            balance = await _balance_from_ledger(db, telegram_id)
            await db.rollback()
            return False, balance
        try:
            await _write_ledger(db, telegram_id, int(diamonds), reason)
        except aiosqlite.IntegrityError:
            balance = await _balance_from_ledger(db, telegram_id)
            await db.rollback()
            return False, balance
        await db.execute(
            "UPDATE users SET updated_at = ? WHERE telegram_id = ?",
            (_now(), telegram_id),
        )
        await db.commit()
        balance = await _balance_from_ledger(db, telegram_id)
    return True, balance


async def is_busy(telegram_id: int) -> bool:
    user = await get_user(telegram_id)
    return bool(user and user.is_busy)
