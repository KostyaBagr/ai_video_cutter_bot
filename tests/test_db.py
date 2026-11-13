"""SQLite user busy lock and ledger-based diamond balance."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from bot import db
from bot.config import settings


def _run(coro):
    return asyncio.run(coro)


def _setup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "test.db"
    monkeypatch.setattr(settings, "sqlite_path", str(path))
    monkeypatch.setattr(settings, "starter_diamonds", 50)
    _run(db.init_db())


def test_upsert_gives_starter_via_ledger(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)

    async def body() -> None:
        user = await db.upsert_user(42, username="alice", first_name="A")
        assert user.telegram_id == 42
        assert user.diamonds == 50
        assert await db.get_balance(42) == 50

        again = await db.upsert_user(42, username="alice2", first_name="B")
        assert again.username == "alice2"
        assert again.diamonds == 50

        async with aiosqlite.connect(settings.sqlite_path) as conn:
            async with conn.execute(
                "SELECT delta, reason FROM ledger WHERE telegram_id = 42"
            ) as cur:
                rows = await cur.fetchall()
        assert rows == [(50, "signup_bonus:42")]

    _run(body())


def test_busy_lock(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)

    async def body() -> None:
        await db.upsert_user(7)
        assert await db.try_set_busy(7) is True
        assert await db.is_busy(7) is True
        assert await db.try_set_busy(7) is False
        await db.clear_busy(7)
        assert await db.is_busy(7) is False
        assert await db.try_set_busy(7) is True

    _run(body())


def test_charge_and_add_via_ledger_deltas(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)

    async def body() -> None:
        await db.upsert_user(9)
        assert await db.get_balance(9) == 50
        assert await db.charge_diamonds(9, 20, reason="job:ai") is True
        assert await db.get_balance(9) == 30
        assert await db.charge_diamonds(9, 40, reason="job:ai") is False
        assert await db.get_balance(9) == 30
        assert await db.add_diamonds(9, 5, reason="test") == 35

        async with aiosqlite.connect(settings.sqlite_path) as conn:
            async with conn.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE telegram_id = 9"
            ) as cur:
                total = int((await cur.fetchone())[0])
        assert total == 35

    _run(body())


def test_backfill_opening_balance_from_old_diamonds_column(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setattr(settings, "sqlite_path", str(path))
    monkeypatch.setattr(settings, "starter_diamonds", 50)

    async def create_legacy() -> None:
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    diamonds INTEGER NOT NULL DEFAULT 0,
                    is_busy INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, last_name,
                    diamonds, is_busy, created_at, updated_at
                ) VALUES (1, 'old', 'O', NULL, 21, 0, 't', 't')
                """
            )
            await conn.commit()

    _run(create_legacy())
    _run(db.init_db())

    async def body() -> None:
        assert await db.get_balance(1) == 21
        user = await db.get_user(1)
        assert user is not None
        assert user.diamonds == 21

    _run(body())


def test_credit_purchase_once_is_idempotent(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch)

    async def body() -> None:
        await db.upsert_user(3)
        ok1, bal1 = await db.credit_purchase_once(
            3, 50, provider_charge_id="chg-1", package_id="s"
        )
        assert ok1 is True
        assert bal1 == 100
        ok2, bal2 = await db.credit_purchase_once(
            3, 50, provider_charge_id="chg-1", package_id="s"
        )
        assert ok2 is False
        assert bal2 == 100

    _run(body())


def test_existing_ledger_not_overwritten_by_cached_column(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "mixed.db"
    monkeypatch.setattr(settings, "sqlite_path", str(path))
    monkeypatch.setattr(settings, "starter_diamonds", 50)

    async def create_mixed() -> None:
        async with aiosqlite.connect(path) as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    diamonds INTEGER NOT NULL DEFAULT 0,
                    is_busy INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, last_name,
                    diamonds, is_busy, created_at, updated_at
                ) VALUES (2, 'x', 'X', NULL, 999, 0, 't', 't')
                """
            )
            await conn.execute(
                """
                INSERT INTO ledger (telegram_id, delta, reason, created_at)
                VALUES (2, 50, 'signup_bonus', 't'),
                       (2, -20, 'job:ai', 't')
                """
            )
            await conn.commit()

    _run(create_mixed())
    _run(db.init_db())

    async def body() -> None:
        # Cached users.diamonds=999 must be ignored; ledger wins
        assert await db.get_balance(2) == 30

    _run(body())
