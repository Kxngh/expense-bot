"""Storage: SQLite by default (one file), or Postgres (e.g. Neon) when DATABASE_URL is set.

All queries are written once, in SQLite style; for Postgres the few differences
(placeholders, INSERT OR IGNORE, schema types) are translated in `_Conn`.
"""
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id       TEXT NOT NULL,
    amount      REAL NOT NULL,
    category    TEXT NOT NULL,
    description TEXT,
    merchant    TEXT,
    spent_on    TEXT NOT NULL,          -- YYYY-MM-DD
    created_at  TEXT NOT NULL,
    source      TEXT DEFAULT 'text'     -- text | image | manual
);
CREATE INDEX IF NOT EXISTS idx_exp_user_date ON expenses (wa_id, spent_on);

CREATE TABLE IF NOT EXISTS budgets (
    wa_id    TEXT NOT NULL,
    category TEXT NOT NULL,             -- a category name, or 'TOTAL'
    amount   REAL NOT NULL,
    PRIMARY KEY (wa_id, category)
);

CREATE TABLE IF NOT EXISTS users (
    wa_id          TEXT PRIMARY KEY,
    last_inbound   TEXT,                -- ISO time of their last message (24h window)
    reports_on     INTEGER NOT NULL DEFAULT 1,
    name           TEXT,                -- WhatsApp profile name
    state          TEXT NOT NULL DEFAULT 'new',   -- new | awaiting_consent | active
    consented_at   TEXT,
    pending_text   TEXT,                -- first message, kept until they reply YES
    pending_action TEXT                 -- e.g. 'delete' while confirming /deletemydata
);

CREATE TABLE IF NOT EXISTS user_categories (
    wa_id TEXT NOT NULL,
    name  TEXT NOT NULL,
    PRIMARY KEY (wa_id, name)
);

CREATE TABLE IF NOT EXISTS reports (
    wa_id      TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- weekly | monthly
    period     TEXT NOT NULL,           -- week start date or YYYY-MM
    status     TEXT NOT NULL,           -- claimed | sent | pending | delivered | skipped
    updated_at TEXT NOT NULL,
    PRIMARY KEY (wa_id, kind, period)
);

CREATE TABLE IF NOT EXISTS processed_messages (
    msg_id  TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
"""

PG_SCHEMA = (SCHEMA
             .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
             .replace("REAL NOT NULL", "DOUBLE PRECISION NOT NULL"))

TOTAL = "TOTAL"

USER_COLUMNS = {
    "name": "TEXT", "state": "TEXT NOT NULL DEFAULT 'new'", "consented_at": "TEXT",
    "pending_text": "TEXT", "pending_action": "TEXT",
}


def is_postgres() -> bool:
    return config.DATABASE_URL.startswith(("postgres://", "postgresql://"))


class _Conn:
    """Runs SQLite-style SQL on either backend."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql: str, params=()):
        if self.pg:
            if sql.lstrip().upper().startswith("INSERT OR IGNORE"):
                sql = re.sub(r"(?i)INSERT OR IGNORE", "INSERT", sql, count=1) + " ON CONFLICT DO NOTHING"
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)


_pool = None
_pool_url = None


def _get_pool():
    """Small pool that drops idle connections after a minute, so Neon can scale to zero."""
    global _pool, _pool_url
    if _pool is None or _pool_url != config.DATABASE_URL:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        if _pool is not None:
            _pool.close()
        _pool = ConnectionPool(
            config.DATABASE_URL, min_size=0, max_size=4, max_idle=60, open=True,
            # prepare_threshold=None keeps it compatible with Neon's pooled (PgBouncer) endpoint
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
        )
        _pool_url = config.DATABASE_URL
    return _pool


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn():
    if is_postgres():
        with _get_pool().connection() as raw:  # commits on success, rolls back on error
            yield _Conn(raw, pg=True)
        return
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield _Conn(c, pg=False)
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        if c.pg:
            for stmt in PG_SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            for col, typ in USER_COLUMNS.items():
                c.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
        else:
            c.raw.executescript(SCHEMA)
            # upgrade databases created by older versions
            have = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            for col, typ in USER_COLUMNS.items():
                if col not in have:
                    c.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
        # people who were already logging before onboarding existed count as active
        c.execute("UPDATE users SET state='active' WHERE state='new' AND wa_id IN (SELECT DISTINCT wa_id FROM expenses)")


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> date:
    return now().date()


# ---------- dedupe (Meta retries webhooks) ----------
def mark_processed(msg_id: str) -> bool:
    """Returns False if this message was already handled."""
    with conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO processed_messages VALUES (?, ?)", (msg_id, now().isoformat()))
        return cur.rowcount == 1


# ---------- expenses ----------
def add_expense(wa_id, amount, category, description="", merchant="", spent_on=None, source="text") -> int:
    spent_on = spent_on or today().isoformat()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO expenses (wa_id, amount, category, description, merchant, spent_on, created_at, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (wa_id, round(float(amount), 2), category, description, merchant, spent_on,
             now().isoformat(), source),
        )
        return cur.fetchone()["id"]


def get_expense(wa_id, exp_id):
    with conn() as c:
        return c.execute("SELECT * FROM expenses WHERE wa_id=? AND id=?", (wa_id, exp_id)).fetchone()


def delete_expense(wa_id, exp_id):
    row = get_expense(wa_id, exp_id)
    if row:
        with conn() as c:
            c.execute("DELETE FROM expenses WHERE wa_id=? AND id=?", (wa_id, exp_id))
    return row


def last_expense(wa_id):
    with conn() as c:
        return c.execute("SELECT * FROM expenses WHERE wa_id=? ORDER BY id DESC LIMIT 1", (wa_id,)).fetchone()


def update_category(wa_id, exp_id, category) -> bool:
    with conn() as c:
        cur = c.execute("UPDATE expenses SET category=? WHERE wa_id=? AND id=?", (category, wa_id, exp_id))
        return cur.rowcount > 0


EDITABLE = ("amount", "category", "description", "merchant", "spent_on")


def update_expense(wa_id, exp_id, **fields) -> bool:
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    if not fields:
        return False
    if "amount" in fields:
        fields["amount"] = round(float(fields["amount"]), 2)
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        cur = c.execute(f"UPDATE expenses SET {sets} WHERE wa_id=? AND id=?", (*fields.values(), wa_id, exp_id))
        return cur.rowcount > 0


def recent(wa_id, n=10):
    with conn() as c:
        return c.execute(
            "SELECT * FROM expenses WHERE wa_id=? ORDER BY spent_on DESC, id DESC LIMIT ?", (wa_id, n)
        ).fetchall()


def recent_logged(wa_id, n=5):
    """Most recently *logged* entries (by id), whatever date they're for."""
    with conn() as c:
        return c.execute("SELECT * FROM expenses WHERE wa_id=? ORDER BY id DESC LIMIT ?", (wa_id, n)).fetchall()


def between(wa_id, start: date, end: date):
    """All expenses with start <= spent_on < end."""
    with conn() as c:
        return c.execute(
            "SELECT * FROM expenses WHERE wa_id=? AND spent_on>=? AND spent_on<? ORDER BY spent_on, id",
            (wa_id, start.isoformat(), end.isoformat()),
        ).fetchall()


def totals_by_category(wa_id, start: date, end: date) -> dict[str, float]:
    with conn() as c:
        rows = c.execute(
            "SELECT category, SUM(amount) s FROM expenses WHERE wa_id=? AND spent_on>=? AND spent_on<?"
            " GROUP BY category ORDER BY s DESC",
            (wa_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["category"]: r["s"] for r in rows}


def totals_by_day(wa_id, start: date, end: date) -> dict[str, float]:
    with conn() as c:
        rows = c.execute(
            "SELECT spent_on, SUM(amount) s FROM expenses WHERE wa_id=? AND spent_on>=? AND spent_on<?"
            " GROUP BY spent_on",
            (wa_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["spent_on"]: r["s"] for r in rows}


def totals_by_month(wa_id, start: date, end: date) -> dict[str, float]:
    with conn() as c:
        rows = c.execute(
            "SELECT substr(spent_on,1,7) m, SUM(amount) s FROM expenses WHERE wa_id=? AND spent_on>=? AND spent_on<?"
            " GROUP BY m",
            (wa_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["m"]: r["s"] for r in rows}


# ---------- budgets ----------
def set_budget(wa_id, category, amount):
    with conn() as c:
        if amount <= 0:
            c.execute("DELETE FROM budgets WHERE wa_id=? AND category=?", (wa_id, category))
        else:
            c.execute(
                "INSERT INTO budgets VALUES (?, ?, ?) ON CONFLICT(wa_id, category) DO UPDATE SET amount=excluded.amount",
                (wa_id, category, float(amount)),
            )


def get_budgets(wa_id) -> dict[str, float]:
    with conn() as c:
        rows = c.execute("SELECT category, amount FROM budgets WHERE wa_id=?", (wa_id,)).fetchall()
    return {r["category"]: r["amount"] for r in rows}


# ---------- users & the WhatsApp 24h window ----------
def touch_user(wa_id):
    with conn() as c:
        c.execute(
            "INSERT INTO users (wa_id, last_inbound) VALUES (?, ?)"
            " ON CONFLICT(wa_id) DO UPDATE SET last_inbound=excluded.last_inbound",
            (wa_id, now().isoformat()),
        )


def get_user(wa_id):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE wa_id=?", (wa_id,)).fetchone()


def all_users():
    with conn() as c:
        return c.execute("SELECT * FROM users").fetchall()


def set_reports_on(wa_id, on: bool):
    with conn() as c:
        c.execute(
            "INSERT INTO users (wa_id, reports_on) VALUES (?, ?)"
            " ON CONFLICT(wa_id) DO UPDATE SET reports_on=excluded.reports_on",
            (wa_id, int(on)),
        )


# ---------- scheduled reports ----------
def claim_report(wa_id, kind, period) -> bool:
    """Atomically claim a report so it's generated once, even with several workers."""
    with conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO reports VALUES (?, ?, ?, 'claimed', ?)",
            (wa_id, kind, period, now().isoformat()),
        )
        return cur.rowcount == 1


def set_report_status(wa_id, kind, period, status):
    with conn() as c:
        c.execute("UPDATE reports SET status=?, updated_at=? WHERE wa_id=? AND kind=? AND period=?",
                  (status, now().isoformat(), wa_id, kind, period))


def pending_reports(wa_id):
    with conn() as c:
        return c.execute(
            "SELECT * FROM reports WHERE wa_id=? AND status='pending' ORDER BY period", (wa_id,)
        ).fetchall()


def report_status(wa_id, kind, period):
    with conn() as c:
        r = c.execute("SELECT status FROM reports WHERE wa_id=? AND kind=? AND period=?",
                      (wa_id, kind, period)).fetchone()
    return r["status"] if r else None


def update_user(wa_id, **fields):
    allowed = {"name", "state", "consented_at", "pending_text", "pending_action", "reports_on"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO users (wa_id) VALUES (?)", (wa_id,))
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE users SET {sets} WHERE wa_id=?", (*fields.values(), wa_id))


def delete_user_data(wa_id) -> int:
    """Permanently removes everything stored for this person. Returns expenses deleted."""
    with conn() as c:
        n = c.execute("DELETE FROM expenses WHERE wa_id=?", (wa_id,)).rowcount
        for table in ("budgets", "reports", "user_categories", "users"):
            c.execute(f"DELETE FROM {table} WHERE wa_id=?", (wa_id,))
    return n


# ---------- custom categories ----------
def custom_categories(wa_id) -> list[str]:
    with conn() as c:
        return [r["name"] for r in c.execute(
            "SELECT name FROM user_categories WHERE wa_id=? ORDER BY name", (wa_id,))]


def add_category(wa_id, name):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO user_categories VALUES (?, ?)", (wa_id, name))


def remove_category(wa_id, name) -> int:
    """Deletes a custom category; its expenses move to Other. Returns how many moved."""
    with conn() as c:
        c.execute("DELETE FROM user_categories WHERE wa_id=? AND name=?", (wa_id, name))
        c.execute("DELETE FROM budgets WHERE wa_id=? AND category=?", (wa_id, name))
        return c.execute("UPDATE expenses SET category='Other' WHERE wa_id=? AND category=?",
                         (wa_id, name)).rowcount
