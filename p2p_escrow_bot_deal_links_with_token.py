"""
P2P Telegram bot with an escrow-like ledger (single-file example).

Stack:
    Python 3.10+
    aiogram 3.x
    SQLite from the Python standard library

Install:
    pip install -U aiogram

Run:
    python p2p_escrow_bot.py

    Optional overrides:
    export BOT_TOKEN="123456:ABC..."
    export ADMIN_IDS="111111111,222222222"

Important:
    This file implements an internal accounting/hold system. It DOES NOT move real
    crypto/fiat funds. For production escrow you must integrate a licensed payment
    or custody provider, add KYC/AML/legal review, audits, backups and monitoring.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from typing import Iterable, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

DB_PATH = os.getenv("DB_PATH", "p2p_escrow.sqlite3")

# Defaults for simple local launch. Environment variables still have priority.
# Keep this file private: it contains your Telegram bot token.
DEFAULT_BOT_TOKEN = "8618134112:AAEezuCB_GshUOeDOnJJTYFsgnybsdckETc"
DEFAULT_ADMIN_IDS = "7576875380"

# Secret code for hidden admin login. Change it before public launch.
# You can also override it without editing the file:
#   PowerShell: $env:ADMIN_CODE="your-long-secret-code"
DEFAULT_ADMIN_CODE = "CLZ-3UqmQwzJEhTagqIIki38qeRxKIze84gvUBg2"
ADMIN_CODE = os.getenv("ADMIN_CODE", DEFAULT_ADMIN_CODE).strip()
MAX_ADMIN_CODE_ATTEMPTS = int(os.getenv("MAX_ADMIN_CODE_ATTEMPTS", "5"))
ADMIN_CODE_LOCK_MINUTES = int(os.getenv("ADMIN_CODE_LOCK_MINUTES", "15"))
_admin_code_attempts: dict[int, tuple[int, datetime]] = {}

SUPPORT_USERNAME = "SafeDealSupport"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"

# Only these currencies are allowed in offers, balances and admin credits.
SUPPORTED_CURRENCIES = [
    ("USDT", "USDT"),
    ("TON", "TON"),
    ("STARS", "Stars"),
    ("RUB", "₽"),
    ("BYN", "бел. ₽"),
    ("KZT", "тенге"),
    ("UAH", "гривны"),
]
CURRENCY_LABELS = dict(SUPPORTED_CURRENCIES)
CURRENCY_ALIASES = {
    "USDT": "USDT",
    "TON": "TON",
    "STARS": "STARS",
    "STAR": "STARS",
    "ЗВЕЗДЫ": "STARS",
    "ЗВЁЗДЫ": "STARS",
    "RUB": "RUB",
    "РУБ": "RUB",
    "РУБЛЬ": "RUB",
    "РУБЛИ": "RUB",
    "₽": "RUB",
    "BYN": "BYN",
    "БЕЛ": "BYN",
    "БЕЛ. Р": "BYN",
    "БЕЛ. ₽": "BYN",
    "БЕЛ РУБ": "BYN",
    "БЕЛ РУБЛЬ": "BYN",
    "KZT": "KZT",
    "ТЕНГЕ": "KZT",
    "UAH": "UAH",
    "ГРИВНА": "UAH",
    "ГРИВНЫ": "UAH",
}

LANGUAGES = {
    "ru": "Русский",
    "en": "English",
}

BOT_TOKEN = os.getenv("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", DEFAULT_ADMIN_IDS).split(",")
    if x.strip().isdigit()
}

MONEY_Q = Decimal("0.00000001")
DEAL_TTL_MINUTES = int(os.getenv("DEAL_TTL_MINUTES", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("p2p-escrow-bot")
router = Router()


class OfferStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class DealStatus(str, Enum):
    ESCROW_LOCKED = "escrow_locked"      # crypto is held by internal ledger
    PAID = "paid"                        # buyer says fiat was paid
    RELEASED = "released"                # seller released escrow to buyer
    CANCELLED = "cancelled"              # cancelled before release
    DISPUTED = "disputed"                # admin must resolve
    RESOLVED_BUYER = "resolved_buyer"    # admin released to buyer
    RESOLVED_SELLER = "resolved_seller"  # admin returned to seller


class SellForm(StatesGroup):
    asset = State()
    product_name = State()
    amount = State()
    payment_details = State()


class BuyForm(StatesGroup):
    amount = State()


@dataclass(frozen=True)
class Offer:
    id: int
    seller_id: int
    product_name: str
    asset: str
    fiat: str
    amount_total: Decimal
    amount_remaining: Decimal
    price: Decimal
    min_limit: Decimal
    max_limit: Decimal
    payment_details: str
    status: OfferStatus


@dataclass(frozen=True)
class Deal:
    id: int
    offer_id: int
    buyer_id: int
    seller_id: int
    product_name: str
    asset: str
    fiat: str
    asset_amount: Decimal
    fiat_amount: Decimal
    price: Decimal
    payment_details: str
    status: DealStatus
    expires_at: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def money(value: Decimal | str | int | float) -> Decimal:
    """Parse and normalize Decimal amounts to 8 decimals."""
    try:
        d = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("Введите корректное число") from None
    if d <= 0:
        raise ValueError("Число должно быть больше 0")
    return d.quantize(MONEY_Q, rounding=ROUND_DOWN)


def fmt(d: Decimal) -> str:
    s = format(d.quantize(MONEY_Q, rounding=ROUND_DOWN), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def normalize_currency(value: str) -> str:
    key = str(value).strip().upper().replace("Ё", "Е")
    code = CURRENCY_ALIASES.get(key)
    if not code:
        raise ValueError(
            "Разрешены только валюты: "
            + ", ".join(label for _code, label in SUPPORTED_CURRENCIES)
        )
    return code


def display_currency(code: str) -> str:
    return CURRENCY_LABELS.get(str(code).upper(), str(code))


def currency_html(code: str) -> str:
    return html_escape(display_currency(code))


def currency_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"{prefix}:{code}")
        for code, label in SUPPORTED_CURRENCIES
    ]
    rows = list(chunked(buttons, 2))
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=name, callback_data=f"lang:{code}")]
            for code, name in LANGUAGES.items()
        ] + [[InlineKeyboardButton(text="Назад", callback_data="menu")]]
    )


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить через @SafeDealSupport", url=SUPPORT_URL)],
            [InlineKeyboardButton(text="Назад в меню", callback_data="menu")],
        ]
    )


def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        return db.has_code_admin_access(user_id)
    except Exception:
        return False


def check_admin_code(user_id: int, code: str) -> tuple[bool, str]:
    """Validate hidden admin code with a simple in-memory brute-force limiter."""
    now = utcnow()
    attempts, locked_until = _admin_code_attempts.get(user_id, (0, datetime.fromtimestamp(0, tz=timezone.utc)))
    if locked_until > now:
        minutes_left = max(1, int((locked_until - now).total_seconds() // 60) + 1)
        return False, f"Слишком много попыток. Повторите через {minutes_left} мин."

    if ADMIN_CODE and hmac.compare_digest(code.strip(), ADMIN_CODE):
        _admin_code_attempts.pop(user_id, None)
        return True, "ok"

    attempts += 1
    if attempts >= MAX_ADMIN_CODE_ATTEMPTS:
        _admin_code_attempts[user_id] = (0, now + timedelta(minutes=ADMIN_CODE_LOCK_MINUTES))
        return False, f"Неверный код. Вход заблокирован на {ADMIN_CODE_LOCK_MINUTES} мин."

    _admin_code_attempts[user_id] = (attempts, datetime.fromtimestamp(0, tz=timezone.utc))
    left = MAX_ADMIN_CODE_ATTEMPTS - attempts
    return False, f"Неверный код. Осталось попыток: {left}."


def chunked(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def tx(self):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            yield con

    def init(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    tg_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    language TEXT NOT NULL DEFAULT 'ru',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS balances (
                    user_id INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    available TEXT NOT NULL DEFAULT '0',
                    locked TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY (user_id, asset),
                    FOREIGN KEY (user_id) REFERENCES users(tg_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS admin_access (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    granted_by TEXT NOT NULL DEFAULT 'code',
                    FOREIGN KEY (user_id) REFERENCES users(tg_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL DEFAULT 'Товар',
                    asset TEXT NOT NULL,
                    fiat TEXT NOT NULL,
                    amount_total TEXT NOT NULL,
                    amount_remaining TEXT NOT NULL,
                    price TEXT NOT NULL,
                    min_limit TEXT NOT NULL,
                    max_limit TEXT NOT NULL,
                    payment_details TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (seller_id) REFERENCES users(tg_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS deals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id INTEGER NOT NULL,
                    buyer_id INTEGER NOT NULL,
                    seller_id INTEGER NOT NULL,
                    product_name TEXT NOT NULL DEFAULT 'Товар',
                    asset TEXT NOT NULL,
                    fiat TEXT NOT NULL,
                    asset_amount TEXT NOT NULL,
                    fiat_amount TEXT NOT NULL,
                    price TEXT NOT NULL,
                    payment_details TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    paid_at TEXT,
                    released_at TEXT,
                    FOREIGN KEY (offer_id) REFERENCES offers(id),
                    FOREIGN KEY (buyer_id) REFERENCES users(tg_id) ON DELETE CASCADE,
                    FOREIGN KEY (seller_id) REFERENCES users(tg_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_offers_status ON offers(status);
                CREATE INDEX IF NOT EXISTS idx_deals_buyer ON deals(buyer_id);
                CREATE INDEX IF NOT EXISTS idx_deals_seller ON deals(seller_id);
                CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);
                """
            )
            self._ensure_user_schema(con)

    def _ensure_user_schema(self, con: sqlite3.Connection) -> None:
        columns = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        if "language" not in columns:
            con.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'ru'")

        offer_columns = {row["name"] for row in con.execute("PRAGMA table_info(offers)").fetchall()}
        if "product_name" not in offer_columns:
            con.execute("ALTER TABLE offers ADD COLUMN product_name TEXT NOT NULL DEFAULT 'Товар'")

        deal_columns = {row["name"] for row in con.execute("PRAGMA table_info(deals)").fetchall()}
        if "product_name" not in deal_columns:
            con.execute("ALTER TABLE deals ADD COLUMN product_name TEXT NOT NULL DEFAULT 'Товар'")

    def upsert_user(self, message_or_callback) -> None:
        user = message_or_callback.from_user
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO users(tg_id, username, first_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name
                """,
                (user.id, user.username, user.first_name, dt_to_str(utcnow())),
            )

    def ensure_balance_row(self, con: sqlite3.Connection, user_id: int, asset: str) -> None:
        asset = normalize_currency(asset)
        con.execute(
            "INSERT OR IGNORE INTO balances(user_id, asset, available, locked) VALUES (?, ?, '0', '0')",
            (user_id, asset),
        )

    def get_balance(self, user_id: int, asset: str) -> tuple[Decimal, Decimal]:
        asset = normalize_currency(asset)
        with self.connect() as con:
            self.ensure_balance_row(con, user_id, asset)
            row = con.execute(
                "SELECT available, locked FROM balances WHERE user_id=? AND asset=?",
                (user_id, asset),
            ).fetchone()
        return Decimal(row["available"]), Decimal(row["locked"])

    def list_balances(self, user_id: int) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(
                con.execute(
                    "SELECT asset, available, locked FROM balances WHERE user_id=? ORDER BY asset",
                    (user_id,),
                ).fetchall()
            )

    def set_language(self, user_id: int, language: str) -> None:
        if language not in LANGUAGES:
            raise ValueError("Такого языка нет")
        with self.connect() as con:
            con.execute("UPDATE users SET language=? WHERE tg_id=?", (language, user_id))

    def get_language(self, user_id: int) -> str:
        with self.connect() as con:
            row = con.execute("SELECT language FROM users WHERE tg_id=?", (user_id,)).fetchone()
        return row["language"] if row and row["language"] in LANGUAGES else "ru"

    def grant_code_admin_access(self, user_id: int) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO admin_access(user_id, created_at, granted_by) VALUES (?, ?, 'code')",
                (user_id, dt_to_str(utcnow())),
            )

    def has_code_admin_access(self, user_id: int) -> bool:
        with self.connect() as con:
            row = con.execute("SELECT 1 FROM admin_access WHERE user_id=?", (user_id,)).fetchone()
        return row is not None

    def revoke_code_admin_access(self, user_id: int) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM admin_access WHERE user_id=?", (user_id,))

    def profile_stats(self, user_id: int) -> dict[str, object]:
        final_statuses = (
            DealStatus.RELEASED.value,
            DealStatus.RESOLVED_BUYER.value,
            DealStatus.RESOLVED_SELLER.value,
        )
        with self.connect() as con:
            user = con.execute(
                "SELECT tg_id, username, first_name, language, created_at FROM users WHERE tg_id=?",
                (user_id,),
            ).fetchone()
            active_deals = con.execute(
                """
                SELECT COUNT(*) AS c FROM deals
                WHERE (buyer_id=? OR seller_id=?) AND status IN (?, ?, ?)
                """,
                (
                    user_id,
                    user_id,
                    DealStatus.ESCROW_LOCKED.value,
                    DealStatus.PAID.value,
                    DealStatus.DISPUTED.value,
                ),
            ).fetchone()["c"]
            placeholders = ",".join("?" for _ in final_statuses)
            successful_deals = con.execute(
                f"""
                SELECT COUNT(*) AS c FROM deals
                WHERE (buyer_id=? OR seller_id=?) AND status IN ({placeholders})
                """,
                (user_id, user_id, *final_statuses),
            ).fetchone()["c"]
            offers = con.execute(
                "SELECT COUNT(*) AS c FROM offers WHERE seller_id=? AND status=?",
                (user_id, OfferStatus.ACTIVE.value),
            ).fetchone()["c"]
        return {
            "user": dict(user) if user else None,
            "active_deals": int(active_deals),
            "successful_deals": int(successful_deals),
            "active_offers": int(offers),
        }

    def credit(self, user_id: int, asset: str, amount: Decimal) -> None:
        asset = normalize_currency(asset)
        with self.tx() as con:
            self.ensure_balance_row(con, user_id, asset)
            row = con.execute(
                "SELECT available FROM balances WHERE user_id=? AND asset=?",
                (user_id, asset),
            ).fetchone()
            available = Decimal(row["available"]) + amount
            con.execute(
                "UPDATE balances SET available=? WHERE user_id=? AND asset=?",
                (fmt(available), user_id, asset),
            )

    def create_offer(
        self,
        seller_id: int,
        product_name: str,
        asset: str,
        fiat: str,
        amount_total: Decimal,
        price: Decimal,
        min_limit: Decimal,
        max_limit: Decimal,
        payment_details: str,
    ) -> int:
        asset = normalize_currency(asset)
        fiat = normalize_currency(fiat)
        product_name = str(product_name).strip()[:120] or "Товар"
        if min_limit > max_limit:
            raise ValueError("Минимальный лимит не может быть больше максимального")
        if amount_total < min_limit:
            raise ValueError("Объём оффера меньше минимального лимита")

        with self.tx() as con:
            self.ensure_balance_row(con, seller_id, asset)
            row = con.execute(
                "SELECT available, locked FROM balances WHERE user_id=? AND asset=?",
                (seller_id, asset),
            ).fetchone()
            available = Decimal(row["available"])
            locked = Decimal(row["locked"])
            if available < amount_total:
                raise ValueError(
                    f"Недостаточно баланса {asset}. Доступно: {fmt(available)}, нужно: {fmt(amount_total)}"
                )

            con.execute(
                "UPDATE balances SET available=?, locked=? WHERE user_id=? AND asset=?",
                (fmt(available - amount_total), fmt(locked + amount_total), seller_id, asset),
            )
            cur = con.execute(
                """
                INSERT INTO offers(
                    seller_id, product_name, asset, fiat, amount_total, amount_remaining, price,
                    min_limit, max_limit, payment_details, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    seller_id,
                    product_name,
                    asset,
                    fiat,
                    fmt(amount_total),
                    fmt(amount_total),
                    fmt(price),
                    fmt(min_limit),
                    fmt(max_limit),
                    payment_details,
                    OfferStatus.ACTIVE.value,
                    dt_to_str(utcnow()),
                ),
            )
            return int(cur.lastrowid)

    def list_active_offers(self, limit: int = 20) -> list[Offer]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM offers
                WHERE status=? AND CAST(amount_remaining AS REAL) > 0
                ORDER BY id DESC
                LIMIT ?
                """,
                (OfferStatus.ACTIVE.value, limit),
            ).fetchall()
        return [self._offer_from_row(r) for r in rows]

    def get_offer(self, offer_id: int) -> Optional[Offer]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
        return self._offer_from_row(row) if row else None

    def cancel_offer(self, offer_id: int, seller_id: int) -> Decimal:
        with self.tx() as con:
            row = con.execute(
                "SELECT * FROM offers WHERE id=? AND seller_id=?",
                (offer_id, seller_id),
            ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            offer = self._offer_from_row(row)
            if offer.status != OfferStatus.ACTIVE:
                raise ValueError("Оффер уже не активен")

            leftover = offer.amount_remaining
            if leftover > 0:
                bal = con.execute(
                    "SELECT available, locked FROM balances WHERE user_id=? AND asset=?",
                    (seller_id, offer.asset),
                ).fetchone()
                available = Decimal(bal["available"])
                locked = Decimal(bal["locked"])
                con.execute(
                    "UPDATE balances SET available=?, locked=? WHERE user_id=? AND asset=?",
                    (fmt(available + leftover), fmt(locked - leftover), seller_id, offer.asset),
                )
            con.execute(
                "UPDATE offers SET status=?, amount_remaining='0' WHERE id=?",
                (OfferStatus.CANCELLED.value, offer_id),
            )
            return leftover

    def create_deal(self, buyer_id: int, offer_id: int, asset_amount: Decimal) -> int:
        with self.tx() as con:
            row = con.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            offer = self._offer_from_row(row)
            if offer.status != OfferStatus.ACTIVE:
                raise ValueError("Оффер не активен")
            if offer.seller_id == buyer_id:
                raise ValueError("Нельзя присоединиться к собственной сделке")
            if asset_amount < offer.min_limit:
                raise ValueError(f"Минимум для сделки: {fmt(offer.min_limit)} {offer.asset}")
            if asset_amount > offer.max_limit:
                raise ValueError(f"Максимум для сделки: {fmt(offer.max_limit)} {offer.asset}")
            if asset_amount > offer.amount_remaining:
                raise ValueError(f"В оффере осталось только {fmt(offer.amount_remaining)} {offer.asset}")

            new_remaining = offer.amount_remaining - asset_amount
            con.execute(
                "UPDATE offers SET amount_remaining=?, status=? WHERE id=?",
                (
                    fmt(new_remaining),
                    OfferStatus.CLOSED.value if new_remaining <= 0 else OfferStatus.ACTIVE.value,
                    offer_id,
                ),
            )
            fiat_amount = (asset_amount * offer.price).quantize(MONEY_Q, rounding=ROUND_DOWN)
            cur = con.execute(
                """
                INSERT INTO deals(
                    offer_id, buyer_id, seller_id, product_name, asset, fiat, asset_amount, fiat_amount,
                    price, payment_details, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    offer.id,
                    buyer_id,
                    offer.seller_id,
                    offer.product_name,
                    offer.asset,
                    offer.fiat,
                    fmt(asset_amount),
                    fmt(fiat_amount),
                    fmt(offer.price),
                    offer.payment_details,
                    DealStatus.ESCROW_LOCKED.value,
                    dt_to_str(utcnow()),
                    dt_to_str(utcnow() + timedelta(minutes=DEAL_TTL_MINUTES)),
                ),
            )
            return int(cur.lastrowid)

    def get_deal(self, deal_id: int) -> Optional[Deal]:
        with self.connect() as con:
            row = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        return self._deal_from_row(row) if row else None

    def list_user_deals(self, user_id: int, limit: int = 10) -> list[Deal]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT * FROM deals
                WHERE buyer_id=? OR seller_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, user_id, limit),
            ).fetchall()
        return [self._deal_from_row(r) for r in rows]

    def list_disputed_deals(self, limit: int = 20) -> list[Deal]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM deals WHERE status=? ORDER BY id DESC LIMIT ?",
                (DealStatus.DISPUTED.value, limit),
            ).fetchall()
        return [self._deal_from_row(r) for r in rows]

    def mark_paid(self, deal_id: int, buyer_id: int) -> Deal:
        with self.tx() as con:
            row = con.execute(
                "SELECT * FROM deals WHERE id=? AND buyer_id=?",
                (deal_id, buyer_id),
            ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            deal = self._deal_from_row(row)
            if deal.status != DealStatus.ESCROW_LOCKED:
                raise ValueError("Сделку нельзя отметить как оплаченную в текущем статусе")
            con.execute(
                "UPDATE deals SET status=?, paid_at=? WHERE id=?",
                (DealStatus.PAID.value, dt_to_str(utcnow()), deal_id),
            )
        updated = self.get_deal(deal_id)
        assert updated is not None
        return updated

    def open_dispute(self, deal_id: int, user_id: int) -> Deal:
        with self.tx() as con:
            row = con.execute(
                "SELECT * FROM deals WHERE id=? AND (buyer_id=? OR seller_id=?)",
                (deal_id, user_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            deal = self._deal_from_row(row)
            if deal.status not in {DealStatus.ESCROW_LOCKED, DealStatus.PAID}:
                raise ValueError("Спор можно открыть только по активной сделке")
            con.execute(
                "UPDATE deals SET status=? WHERE id=?",
                (DealStatus.DISPUTED.value, deal_id),
            )
        updated = self.get_deal(deal_id)
        assert updated is not None
        return updated

    def cancel_deal_by_buyer(self, deal_id: int, buyer_id: int) -> Deal:
        """Buyer can cancel only before marking the deal as paid."""
        with self.tx() as con:
            row = con.execute(
                "SELECT * FROM deals WHERE id=? AND buyer_id=?",
                (deal_id, buyer_id),
            ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            deal = self._deal_from_row(row)
            if deal.status != DealStatus.ESCROW_LOCKED:
                raise ValueError("Отменить можно только сделку до подтверждения оплаты")
            self._return_deal_to_offer_or_seller(con, deal)
            con.execute(
                "UPDATE deals SET status=? WHERE id=?",
                (DealStatus.CANCELLED.value, deal_id),
            )
        updated = self.get_deal(deal_id)
        assert updated is not None
        return updated

    def release_to_buyer(self, deal_id: int, seller_id: int | None = None, admin: bool = False) -> Deal:
        with self.tx() as con:
            if admin:
                row = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM deals WHERE id=? AND seller_id=?",
                    (deal_id, seller_id),
                ).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            deal = self._deal_from_row(row)
            if deal.status not in {DealStatus.PAID, DealStatus.DISPUTED}:
                raise ValueError("Релиз доступен после оплаты или в споре")

            self.ensure_balance_row(con, deal.buyer_id, deal.asset)
            seller_bal = con.execute(
                "SELECT locked FROM balances WHERE user_id=? AND asset=?",
                (deal.seller_id, deal.asset),
            ).fetchone()
            buyer_bal = con.execute(
                "SELECT available FROM balances WHERE user_id=? AND asset=?",
                (deal.buyer_id, deal.asset),
            ).fetchone()
            seller_locked = Decimal(seller_bal["locked"])
            buyer_available = Decimal(buyer_bal["available"])
            if seller_locked < deal.asset_amount:
                raise ValueError("Внутренняя ошибка: недостаточно locked-баланса продавца")

            con.execute(
                "UPDATE balances SET locked=? WHERE user_id=? AND asset=?",
                (fmt(seller_locked - deal.asset_amount), deal.seller_id, deal.asset),
            )
            con.execute(
                "UPDATE balances SET available=? WHERE user_id=? AND asset=?",
                (fmt(buyer_available + deal.asset_amount), deal.buyer_id, deal.asset),
            )
            con.execute(
                "UPDATE deals SET status=?, released_at=? WHERE id=?",
                (
                    DealStatus.RESOLVED_BUYER.value if admin else DealStatus.RELEASED.value,
                    dt_to_str(utcnow()),
                    deal_id,
                ),
            )
        updated = self.get_deal(deal_id)
        assert updated is not None
        return updated

    def resolve_to_seller(self, deal_id: int) -> Deal:
        with self.tx() as con:
            row = con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
            if not row:
                raise ValueError("Сделка не найдена")
            deal = self._deal_from_row(row)
            if deal.status != DealStatus.DISPUTED:
                raise ValueError("Возврат продавцу через админа доступен только по спору")
            self._return_deal_to_offer_or_seller(con, deal, force_return_to_seller=True)
            con.execute(
                "UPDATE deals SET status=?, released_at=? WHERE id=?",
                (DealStatus.RESOLVED_SELLER.value, dt_to_str(utcnow()), deal_id),
            )
        updated = self.get_deal(deal_id)
        assert updated is not None
        return updated

    def _return_deal_to_offer_or_seller(
        self,
        con: sqlite3.Connection,
        deal: Deal,
        force_return_to_seller: bool = False,
    ) -> None:
        offer_row = con.execute("SELECT * FROM offers WHERE id=?", (deal.offer_id,)).fetchone()
        seller_bal = con.execute(
            "SELECT available, locked FROM balances WHERE user_id=? AND asset=?",
            (deal.seller_id, deal.asset),
        ).fetchone()
        available = Decimal(seller_bal["available"])
        locked = Decimal(seller_bal["locked"])

        if locked < deal.asset_amount:
            raise ValueError("Внутренняя ошибка: недостаточно locked-баланса продавца")

        if not force_return_to_seller and offer_row:
            offer = self._offer_from_row(offer_row)
            if offer.status in {OfferStatus.ACTIVE, OfferStatus.CLOSED}:
                new_remaining = offer.amount_remaining + deal.asset_amount
                con.execute(
                    "UPDATE offers SET amount_remaining=?, status=? WHERE id=?",
                    (fmt(new_remaining), OfferStatus.ACTIVE.value, deal.offer_id),
                )
                return

        con.execute(
            "UPDATE balances SET available=?, locked=? WHERE user_id=? AND asset=?",
            (fmt(available + deal.asset_amount), fmt(locked - deal.asset_amount), deal.seller_id, deal.asset),
        )

    @staticmethod
    def _offer_from_row(row: sqlite3.Row) -> Offer:
        return Offer(
            id=int(row["id"]),
            seller_id=int(row["seller_id"]),
            product_name=row["product_name"] if "product_name" in row.keys() else "Товар",
            asset=row["asset"],
            fiat=row["fiat"],
            amount_total=Decimal(row["amount_total"]),
            amount_remaining=Decimal(row["amount_remaining"]),
            price=Decimal(row["price"]),
            min_limit=Decimal(row["min_limit"]),
            max_limit=Decimal(row["max_limit"]),
            payment_details=row["payment_details"],
            status=OfferStatus(row["status"]),
        )

    @staticmethod
    def _deal_from_row(row: sqlite3.Row) -> Deal:
        return Deal(
            id=int(row["id"]),
            offer_id=int(row["offer_id"]),
            buyer_id=int(row["buyer_id"]),
            seller_id=int(row["seller_id"]),
            product_name=row["product_name"] if "product_name" in row.keys() else "Товар",
            asset=row["asset"],
            fiat=row["fiat"],
            asset_amount=Decimal(row["asset_amount"]),
            fiat_amount=Decimal(row["fiat_amount"]),
            price=Decimal(row["price"]),
            payment_details=row["payment_details"],
            status=DealStatus(row["status"]),
            expires_at=row["expires_at"],
        )


db = Database(DB_PATH)


def main_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📈 Активные сделки", callback_data="offers")],
        [InlineKeyboardButton(text="➕ Создать сделку", callback_data="sell")],
        [
            InlineKeyboardButton(text="💼 Баланс", callback_data="balance"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="🤝 Мои сделки", callback_data="mydeals"),
            InlineKeyboardButton(text="🌐 Язык", callback_data="language"),
        ],
        [InlineKeyboardButton(text="💳 Пополнить баланс", url=SUPPORT_URL)],
    ]
    if user_id is not None and is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offer_keyboard(offer_id: int, seller_id: int, viewer_id: int) -> InlineKeyboardMarkup:
    rows = []
    if viewer_id != seller_id:
        rows.append([InlineKeyboardButton(text="Присоединиться", callback_data=f"buy:{offer_id}")])
    else:
        rows.append([InlineKeyboardButton(text="Закрыть сделку", callback_data=f"cancel_offer:{offer_id}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="offers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def deal_keyboard(deal: Deal, viewer_id: int) -> InlineKeyboardMarkup:
    rows = []
    if viewer_id == deal.buyer_id and deal.status == DealStatus.ESCROW_LOCKED:
        rows.append([InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"paid:{deal.id}")])
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_deal:{deal.id}")])
    if viewer_id == deal.seller_id and deal.status in {DealStatus.PAID, DealStatus.DISPUTED}:
        rows.append([InlineKeyboardButton(text="🔓 Отпустить escrow покупателю", callback_data=f"release:{deal.id}")])
    if viewer_id in {deal.buyer_id, deal.seller_id} and deal.status in {DealStatus.ESCROW_LOCKED, DealStatus.PAID}:
        rows.append([InlineKeyboardButton(text="⚠️ Открыть спор", callback_data=f"dispute:{deal.id}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="mydeals")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offer_text(offer: Offer) -> str:
    product = html_escape(offer.product_name)
    return (
        f"<b>Сделка #{offer.id}</b>\n"
        f"Товар: <b>{product}</b>\n"
        f"Продавец: <code>{offer.seller_id}</code>\n"
        f"Валюта: <b>{currency_html(offer.asset)}</b>\n"
        f"Сумма: <b>{fmt(offer.amount_remaining)}</b> {currency_html(offer.asset)}\n"
        f"Статус: <code>{offer.status.value}</code>"
    )


def deal_join_link(bot_username: str, offer_id: int) -> str:
    return f"https://t.me/{bot_username}?start=deal_{offer_id}"


def deal_text(deal: Deal, for_seller: bool = False) -> str:
    pay = html_escape(deal.payment_details) if not for_seller else "скрыто: реквизиты уже отправлены покупателю"
    return (
        f"<b>Сделка #{deal.id}</b>\n"
        f"Товар: <b>{html_escape(deal.product_name)}</b>\n"
        f"Статус: <code>{deal.status.value}</code>\n"
        f"Покупатель: <code>{deal.buyer_id}</code>\n"
        f"Продавец: <code>{deal.seller_id}</code>\n"
        f"Сумма: <b>{fmt(deal.asset_amount)}</b> {currency_html(deal.asset)}\n"
        f"Escrow: <b>заблокировано у продавца</b>\n"
        f"Истекает: <code>{html_escape(deal.expires_at)}</code>\n\n"
        f"<b>Реквизиты оплаты:</b>\n{pay}"
    )


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramBadRequest as e:
        logger.warning("Cannot send message to %s: %s", chat_id, e)


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, bot: Bot) -> None:
    db.upsert_user(message)
    args = (command.args or "").strip()
    if args.startswith("deal_"):
        try:
            offer_id = int(args.split("_", 1)[1])
        except ValueError:
            await message.answer("Ссылка на сделку повреждена.", reply_markup=main_menu(message.from_user.id))
            return
        await join_offer_as_buyer(message, offer_id, bot)
        return

    await message.answer(
        "<b>P2P escrow bot</b>\n\n"
        "Создай сделку, выбери валюту, укажи название товара и сумму — бот выдаст ссылку для покупателя.\n\n"
        "Команды:\n"
        "/sell — создать сделку\n"
        "/offers — активные сделки\n"
        "/balance — баланс\n"
        "/mydeals — мои сделки\n"
        "/deposit — пополнение через @SafeDealSupport\n"
        "/profile — профиль\n"
        "/language — смена языка\n\n"
        "Важно: это каркас с внутренним ledger-escrow. Реальные платежи нужно подключать отдельно.",
        reply_markup=main_menu(message.from_user.id),
    )


@router.callback_query(F.data == "balance")
async def cb_balance(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    await show_balance(callback.message, callback.from_user.id)


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    db.upsert_user(message)
    await show_balance(message, message.from_user.id)


async def show_balance(message: Message, user_id: int) -> None:
    rows = db.list_balances(user_id)
    by_asset = {r["asset"]: r for r in rows}
    text = "<b>Ваш баланс</b>\n"
    for code, label in SUPPORTED_CURRENCIES:
        r = by_asset.get(code)
        available = r["available"] if r else "0"
        locked = r["locked"] if r else "0"
        text += (
            f"\n<b>{html_escape(label)}</b>\n"
            f"Доступно: <code>{html_escape(available)}</code>\n"
            f"В escrow/locked: <code>{html_escape(locked)}</code>\n"
        )
    await message.answer(text, reply_markup=support_keyboard())


@router.message(Command("deposit"))
async def cmd_deposit(message: Message) -> None:
    db.upsert_user(message)
    await message.answer(
        "<b>Пополнение баланса</b>\n\n"
        "Для пополнения напишите в поддержку: @SafeDealSupport\n"
        f"Ваш USER_ID для заявки: <code>{message.from_user.id}</code>\n\n"
        "Внутри этого single-file бота реальные депозиты не принимаются: после ручной проверки внешний платёж может начислить только админ.",
        reply_markup=support_keyboard(),
    )


@router.callback_query(F.data == "sell")
@router.message(Command("sell"))
async def start_sell(event: Message | CallbackQuery, state: FSMContext) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
    else:
        db.upsert_user(event)
        message = event
    await state.clear()
    await state.set_state(SellForm.asset)
    await message.answer(
        "Выберите валюту сделки:",
        reply_markup=currency_keyboard("sell_asset"),
    )


@router.callback_query(StateFilter(SellForm.asset), F.data.startswith("sell_asset:"))
async def sell_asset_callback(callback: CallbackQuery, state: FSMContext) -> None:
    db.upsert_user(callback)
    await callback.answer()
    asset = callback.data.split(":", 1)[1]
    try:
        asset = normalize_currency(asset)
    except ValueError as e:
        await callback.message.answer(str(e))
        return
    await state.update_data(asset=asset)
    await state.set_state(SellForm.product_name)
    await callback.message.answer("Напишите название товара/услуги для сделки:")


@router.message(SellForm.asset)
async def sell_asset(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Выберите валюту кнопкой ниже:",
        reply_markup=currency_keyboard("sell_asset"),
    )


@router.message(SellForm.product_name)
async def sell_product_name(message: Message, state: FSMContext) -> None:
    product_name = (message.text or "").strip()
    if len(product_name) < 2:
        await message.answer("Название слишком короткое. Напишите название товара/услуги ещё раз.")
        return
    if len(product_name) > 120:
        await message.answer("Название слишком длинное. До 120 символов.")
        return
    await state.update_data(product_name=product_name)
    data = await state.get_data()
    await state.set_state(SellForm.amount)
    await message.answer(f"Введите сумму сделки в {currency_html(data['asset'])}:")


@router.message(SellForm.amount)
async def sell_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = money(message.text)
    except ValueError as e:
        await message.answer(str(e))
        return
    data = await state.get_data()
    available, _locked = db.get_balance(message.from_user.id, data["asset"])
    if available < amount:
        await message.answer(
            f"Недостаточно {currency_html(data['asset'])}. Доступно: <code>{fmt(available)}</code>.\n"
            "Пополнить баланс можно через @SafeDealSupport."
        )
        return
    await state.update_data(amount=str(amount))
    await state.set_state(SellForm.payment_details)
    await message.answer(
        "Введите реквизиты/условия оплаты, которые увидит покупатель.\n"
        "Пример: банк/кошелёк, номер карты/счёта, комментарий к платежу."
    )


@router.message(SellForm.payment_details)
async def sell_payment_details(message: Message, state: FSMContext, bot: Bot) -> None:
    details = (message.text or "").strip()
    if len(details) < 5:
        await message.answer("Реквизиты слишком короткие, введите подробнее.")
        return
    data = await state.get_data()
    amount = Decimal(data["amount"])
    asset = data["asset"]
    try:
        offer_id = db.create_offer(
            seller_id=message.from_user.id,
            product_name=data["product_name"],
            asset=asset,
            fiat=asset,
            amount_total=amount,
            price=Decimal("1"),
            min_limit=amount,
            max_limit=amount,
            payment_details=details,
        )
    except ValueError as e:
        await state.clear()
        await message.answer(f"Не удалось создать сделку: {html_escape(str(e))}")
        return
    await state.clear()
    offer = db.get_offer(offer_id)
    me = await bot.get_me()
    link = deal_join_link(me.username, offer_id)
    await message.answer(
        "✅ Сделка создана. Средства заблокированы в escrow/locked.\n\n"
        + offer_text(offer)
        + "\n\n<b>Ссылка для присоединения покупателя:</b>\n"
        + f"<code>{html_escape(link)}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Открыть ссылку", url=link)],
                [InlineKeyboardButton(text="Закрыть сделку", callback_data=f"cancel_offer:{offer.id}")],
                [InlineKeyboardButton(text="Меню", callback_data="menu")],
            ]
        ),
    )


@router.callback_query(F.data == "offers")
@router.message(Command("offers"))
async def show_offers(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
    else:
        db.upsert_user(event)
        message = event

    offers = db.list_active_offers(limit=30)
    if not offers:
        await message.answer("Активных сделок пока нет.", reply_markup=main_menu())
        return

    buttons = []
    for offer in offers:
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"#{offer.id}: {offer.product_name[:24]} — {fmt(offer.amount_remaining)} {display_currency(offer.asset)}",
                    callback_data=f"offer:{offer.id}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton(text="Меню", callback_data="menu")])
    await message.answer("<b>Активные сделки</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("offer:"))
async def cb_offer(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    offer_id = int(callback.data.split(":", 1)[1])
    offer = db.get_offer(offer_id)
    if not offer:
        await callback.message.answer("Сделка не найдена")
        return
    await callback.message.answer(
        offer_text(offer),
        reply_markup=offer_keyboard(offer.id, offer.seller_id, callback.from_user.id),
    )


@router.callback_query(F.data.startswith("cancel_offer:"))
async def cb_cancel_offer(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    offer_id = int(callback.data.split(":", 1)[1])
    try:
        returned = db.cancel_offer(offer_id, callback.from_user.id)
    except ValueError as e:
        await callback.message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await callback.message.answer(f"Сделка закрыта. Возвращено в доступный баланс: <code>{fmt(returned)}</code>")


async def join_offer_as_buyer(event: Message | CallbackQuery, offer_id: int, bot: Bot) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        message = event.message
        user_id = event.from_user.id
    else:
        db.upsert_user(event)
        message = event
        user_id = event.from_user.id

    offer = db.get_offer(offer_id)
    if not offer or offer.status != OfferStatus.ACTIVE:
        await message.answer("Сделка неактивна или уже занята.", reply_markup=main_menu(user_id))
        return
    if offer.seller_id == user_id:
        await message.answer(
            "Это ваша сделка. Отправьте ссылку покупателю, чтобы он присоединился.",
            reply_markup=offer_keyboard(offer.id, offer.seller_id, user_id),
        )
        return
    try:
        deal_id = db.create_deal(user_id, offer.id, offer.amount_remaining)
    except ValueError as e:
        await message.answer(f"Ошибка: {html_escape(str(e))}", reply_markup=main_menu(user_id))
        return

    deal = db.get_deal(deal_id)
    assert deal is not None
    await message.answer(
        "✅ Вы присоединились к сделке. Оплатите продавцу по реквизитам, затем нажмите «Я оплатил».\n\n"
        + deal_text(deal),
        reply_markup=deal_keyboard(deal, user_id),
    )
    await safe_send(
        bot,
        deal.seller_id,
        "🔔 Покупатель присоединился к вашей сделке. Ждите подтверждения оплаты.\n\n"
        + deal_text(deal, for_seller=True),
        reply_markup=deal_keyboard(deal, deal.seller_id),
    )


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback)
    await callback.answer()
    offer_id = int(callback.data.split(":", 1)[1])
    await join_offer_as_buyer(callback, offer_id, bot)


@router.message(BuyForm.amount)
async def buy_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    # Оставлено для совместимости со старыми состояниями, если они были в памяти.
    await state.clear()
    await message.answer("Откройте ссылку на сделку или выберите активную сделку из списка.", reply_markup=main_menu(message.from_user.id))


@router.callback_query(F.data == "mydeals")
@router.message(Command("mydeals"))
async def show_my_deals(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
        user_id = event.from_user.id
    else:
        db.upsert_user(event)
        message = event
        user_id = event.from_user.id

    deals = db.list_user_deals(user_id, limit=20)
    if not deals:
        await message.answer("У вас пока нет сделок.", reply_markup=main_menu())
        return
    buttons = []
    for d in deals:
        role = "BUY" if d.buyer_id == user_id else "SELL"
        buttons.append(
            [InlineKeyboardButton(text=f"#{d.id} {role}: {fmt(d.asset_amount)} {display_currency(d.asset)} | {d.status.value}", callback_data=f"deal:{d.id}")]
        )
    buttons.append([InlineKeyboardButton(text="Меню", callback_data="menu")])
    await message.answer("<b>Мои сделки</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("deal:"))
async def cb_deal(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    deal_id = int(callback.data.split(":", 1)[1])
    deal = db.get_deal(deal_id)
    if not deal or callback.from_user.id not in {deal.buyer_id, deal.seller_id}:
        await callback.message.answer("Сделка не найдена")
        return
    await callback.message.answer(
        deal_text(deal, for_seller=callback.from_user.id == deal.seller_id),
        reply_markup=deal_keyboard(deal, callback.from_user.id),
    )


@router.callback_query(F.data.startswith("paid:"))
async def cb_paid(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback)
    await callback.answer()
    deal_id = int(callback.data.split(":", 1)[1])
    try:
        deal = db.mark_paid(deal_id, callback.from_user.id)
    except ValueError as e:
        await callback.message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await callback.message.answer("Оплата отмечена. Продавец получил уведомление.", reply_markup=deal_keyboard(deal, callback.from_user.id))
    await safe_send(
        bot,
        deal.seller_id,
        "✅ Покупатель отметил оплату. Проверьте поступление денег. Если всё пришло — отпустите escrow.\n\n"
        + deal_text(deal, for_seller=True),
        reply_markup=deal_keyboard(deal, deal.seller_id),
    )


@router.callback_query(F.data.startswith("release:"))
async def cb_release(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback)
    await callback.answer()
    deal_id = int(callback.data.split(":", 1)[1])
    try:
        deal = db.release_to_buyer(deal_id, seller_id=callback.from_user.id)
    except ValueError as e:
        await callback.message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await callback.message.answer("✅ Escrow отпущен покупателю.")
    await safe_send(
        bot,
        deal.buyer_id,
        "✅ Продавец отпустил escrow. Актив зачислен на ваш внутренний баланс.\n\n" + deal_text(deal),
    )


@router.callback_query(F.data.startswith("cancel_deal:"))
async def cb_cancel_deal(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback)
    await callback.answer()
    deal_id = int(callback.data.split(":", 1)[1])
    try:
        deal = db.cancel_deal_by_buyer(deal_id, callback.from_user.id)
    except ValueError as e:
        await callback.message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await callback.message.answer("Сделка отменена до оплаты.")
    await safe_send(bot, deal.seller_id, f"❌ Покупатель отменил сделку #{deal.id} до оплаты.")


@router.callback_query(F.data.startswith("dispute:"))
async def cb_dispute(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback)
    await callback.answer()
    deal_id = int(callback.data.split(":", 1)[1])
    try:
        deal = db.open_dispute(deal_id, callback.from_user.id)
    except ValueError as e:
        await callback.message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await callback.message.answer("⚠️ Спор открыт. Администратор должен проверить доказательства оплаты.")
    for admin_id in ADMIN_IDS:
        await safe_send(
            bot,
            admin_id,
            "⚠️ Открыт спор.\n\n"
            + deal_text(deal, for_seller=False)
            + "\n\nКоманды:\n"
            + f"<code>/resolve {deal.id} buyer</code> — отдать escrow покупателю\n"
            + f"<code>/resolve {deal.id} seller</code> — вернуть продавцу",
        )


@router.callback_query(F.data == "profile")
@router.message(Command("profile"))
async def show_profile(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
        user = event.from_user
    else:
        db.upsert_user(event)
        message = event
        user = event.from_user

    stats = db.profile_stats(user.id)
    info = stats.get("user") or {}
    username = f"@{info.get('username')}" if info.get("username") else "не указан"
    language = LANGUAGES.get(info.get("language", "ru"), "Русский")
    text = (
        "<b>👤 Профиль</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: <code>{html_escape(username)}</code>\n"
        f"Язык: <b>{html_escape(language)}</b>\n"
        f"Активные офферы: <b>{stats['active_offers']}</b>\n"
        f"Активные сделки: <b>{stats['active_deals']}</b>\n"
        f"Успешные сделки: <b>{stats['successful_deals']}</b>\n\n"
        "Статистика считается только по реальным завершённым сделкам внутри бота."
    )
    await message.answer(text, reply_markup=main_menu(user.id))


@router.callback_query(F.data == "language")
@router.message(Command("language"))
async def show_language(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
    else:
        db.upsert_user(event)
        message = event
    await message.answer("Выберите язык интерфейса:", reply_markup=language_keyboard())


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    lang = callback.data.split(":", 1)[1]
    try:
        db.set_language(callback.from_user.id, lang)
    except ValueError as e:
        await callback.message.answer(str(e))
        return
    await callback.message.answer(f"Язык изменён: <b>{html_escape(LANGUAGES[lang])}</b>", reply_markup=main_menu(callback.from_user.id))


async def send_admin_panel(message: Message) -> None:
    await message.answer(
        "<b>🛠 Админ-панель</b>\n\n"
        "Доступные команды:\n"
        "<code>/credit USER_ID CURRENCY AMOUNT</code> — начислить баланс после ручной проверки\n"
        "<code>/admin_deals</code> — список споров\n"
        "<code>/resolve DEAL_ID buyer</code> — отдать escrow покупателю\n"
        "<code>/resolve DEAL_ID seller</code> — вернуть escrow продавцу\n\n"
        "Валюты: " + ", ".join(html_escape(label) for _code, label in SUPPORTED_CURRENCIES),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚠️ Споры", callback_data="admin_disputes")],
                [InlineKeyboardButton(text="Меню", callback_data="menu")],
            ]
        ),
    )


@router.callback_query(F.data == "admin")
@router.message(Command("admin"))
async def show_admin_panel(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        db.upsert_user(event)
        await event.answer()
        message = event.message
        user_id = event.from_user.id
    else:
        db.upsert_user(event)
        message = event
        user_id = event.from_user.id
    if not is_admin(user_id):
        await message.answer("Нет прав")
        return
    await send_admin_panel(message)


@router.message(Command("clezzy"))
async def cmd_clezzy(message: Message, command: CommandObject) -> None:
    db.upsert_user(message)
    user_id = message.from_user.id

    if is_admin(user_id):
        await send_admin_panel(message)
        return

    code = (command.args or "").strip()
    if not code:
        await message.answer("Неверный формат")
        return

    ok, status = check_admin_code(user_id, code)
    if not ok:
        await message.answer(html_escape(status))
        return

    db.grant_code_admin_access(user_id)
    await message.answer("✅ Доступ открыт.")
    await send_admin_panel(message)

@router.callback_query(F.data == "admin_disputes")
async def cb_admin_disputes(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("Нет прав")
        return
    deals = db.list_disputed_deals()
    if not deals:
        await callback.message.answer("Споров нет", reply_markup=main_menu(callback.from_user.id))
        return
    text = "<b>Спорные сделки</b>\n\n"
    for d in deals:
        text += f"#{d.id}: {fmt(d.asset_amount)} {currency_html(d.asset)} | buyer {d.buyer_id} | seller {d.seller_id}\n"
    text += "\n<code>/resolve DEAL_ID buyer</code> или <code>/resolve DEAL_ID seller</code>"
    await callback.message.answer(text)


@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery) -> None:
    db.upsert_user(callback)
    await callback.answer()
    await callback.message.answer("Меню", reply_markup=main_menu(callback.from_user.id))


@router.message(Command("credit"))
async def cmd_credit(message: Message, command: CommandObject) -> None:
    db.upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет прав")
        return
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[0].isdigit():
        await message.answer(
            "Формат: <code>/credit USER_ID CURRENCY AMOUNT</code>\n"
            "Валюты: " + ", ".join(html_escape(label) for _code, label in SUPPORTED_CURRENCIES)
        )
        return
    user_id = int(parts[0])
    try:
        asset = normalize_currency(parts[1])
        amount = money(parts[2])
    except ValueError as e:
        await message.answer(str(e))
        return
    with db.connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, first_name, created_at) VALUES (?, '', '', ?)",
            (user_id, dt_to_str(utcnow())),
        )
    db.credit(user_id, asset, amount)
    await message.answer(f"Начислено: <code>{fmt(amount)} {currency_html(asset)}</code> пользователю <code>{user_id}</code>")


@router.message(Command("admin_deals"))
async def cmd_admin_deals(message: Message) -> None:
    db.upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет прав")
        return
    deals = db.list_disputed_deals()
    if not deals:
        await message.answer("Споров нет")
        return
    text = "<b>Спорные сделки</b>\n\n"
    for d in deals:
        text += f"#{d.id}: {fmt(d.asset_amount)} {html_escape(d.asset)} | buyer {d.buyer_id} | seller {d.seller_id}\n"
    text += "\n<code>/resolve DEAL_ID buyer</code> или <code>/resolve DEAL_ID seller</code>"
    await message.answer(text)


@router.message(Command("resolve"))
async def cmd_resolve(message: Message, command: CommandObject, bot: Bot) -> None:
    db.upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет прав")
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in {"buyer", "seller"}:
        await message.answer("Формат: <code>/resolve DEAL_ID buyer</code> или <code>/resolve DEAL_ID seller</code>")
        return
    deal_id = int(parts[0])
    target = parts[1]
    try:
        if target == "buyer":
            deal = db.release_to_buyer(deal_id, admin=True)
            result = "escrow отдан покупателю"
        else:
            deal = db.resolve_to_seller(deal_id)
            result = "escrow возвращён продавцу"
    except ValueError as e:
        await message.answer(f"Ошибка: {html_escape(str(e))}")
        return
    await message.answer(f"Готово: сделка #{deal.id}, {result}.")
    await safe_send(bot, deal.buyer_id, f"Спор по сделке #{deal.id} закрыт: {result}.")
    await safe_send(bot, deal.seller_id, f"Спор по сделке #{deal.id} закрыт: {result}.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(StateFilter("*"))
async def fallback(message: Message) -> None:
    db.upsert_user(message)
    await message.answer("Не понял команду. Используйте /start", reply_markup=main_menu())


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty. Hidden admin access is still possible if ADMIN_CODE is set.")
    db.init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Bot started. DB=%s admins=%s", DB_PATH, sorted(ADMIN_IDS))
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
