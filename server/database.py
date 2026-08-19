import math
import psycopg2
import psycopg2.extras
import psycopg2.extensions
import psycopg2.pool
import threading
import uuid
import os
import json
import calendar
import contextvars
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

# Set by server.py's _bind_db_identity ASGI middleware for the duration of a
# request (must be middleware, not a FastAPI dependency — see
# auth.resolve_identity_for_db's docstring for why). get_connection() reads
# this to bind the checked-out connection to the requesting user's Postgres
# identity so RLS policies actually apply. Left unset (None) for
# system/startup code — init_db(), backfill_all_users(), seed_demo.py — which
# legitimately runs outside any one user's request and should keep using the
# app's own bypass-RLS role.
current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)

# Every table with a user_id column — the single source of truth for "which
# tables hold one user's data." Used to RLS-enable every one of them (below)
# and by tests/conftest.py's session teardown, which needs to wipe all of
# them for full test-to-test isolation. Deliberately NOT used by
# auth_router.py's clear_all_data endpoint, which is a curated *subset*
# (transactional data only, not account preferences like user_settings) —
# that's a real, deliberate scope difference, not drift to unify away. Keep
# this list in sync with init_db()'s CREATE TABLE statements when adding a
# new user-scoped table.
_USER_OWNED_TABLES = [
    "expenses", "incomes", "expense_types", "macrocategories", "budgets",
    "monthly_budgets", "import_rules", "savings_goals", "savings_contributions",
    "user_settings", "transaction_embeddings",
]


# Aligned with Plaid's personal_finance_category.primary taxonomy (see
# PLAID_CATEGORY_HINTS in server/plaid_client.py on feature/plaid-integration) so a
# synced Plaid transaction lands in an existing default category instead of
# triggering the auto-create-near-duplicate fallback. "Other" stays the catch-all
# fallback _infer_type_with_source() depends on; not a Plaid category itself.
_DEFAULT_TYPES = [
    ("Food & Drink",             "#e8a87c", "Restaurant",     0),
    ("Transportation",           "#82b4e0", "Commute",        1),
    ("Rent & Utilities",         "#c49ee8", "Home",           2),
    ("Entertainment",            "#f0c040", "Movie",          3),
    ("Medical",                  "#80cbc4", "LocalHospital",  4),
    ("Other",                    "#a0a0a0", "Category",       5),
    ("Travel",                   "#90caf9", "Flight",         6),
    ("Home Improvement",         "#ff8a65", "Build",          7),
    ("Shopping",                 "#a5d6a7", "ShoppingCart",   8),
    ("Personal Care",            "#f48fb1", "FitnessCenter",  9),
    ("Government & Non-Profit",  "#ce93d8", "Landmark",      10),
]


def _connect_kwargs():
    return dict(
        dsn=os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=10,
        keepalives_interval=5,
        keepalives_count=3,
    )


def _make_raw_conn():
    return psycopg2.connect(**_connect_kwargs())


# Every request opened (and fully tore down) its own TCP+TLS+Postgres-auth
# connection before this pool existed — a fixed per-request tax regardless of
# how close the caller is to the DB. Pooling amortizes that handshake cost
# across requests instead of paying it every time. Sized modestly: Supabase's
# transaction pooler (Supavisor) is meant for many short-lived clients, but
# there's no reason for a single Render instance to hold more than a handful
# of connections open at once under normal load.
_POOL_MIN = 2
_POOL_MAX = 20
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, **_connect_kwargs())
    return _pool


def _release(conn):
    pool = _get_pool()
    try:
        if conn.closed:
            pool.putconn(conn, close=True)
            return
        # A handler that raised/returned without an explicit commit() or
        # rollback() would otherwise hand back a connection mid-transaction
        # (or aborted) — the next borrower's first query would silently run
        # inside stale state, or fail outright. Always leave it idle.
        if conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
            conn.rollback()
        pool.putconn(conn)
    except Exception:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass


class _ReconnectingCursor:
    """Cursor wrapper that retries once on dropped SSL connections."""

    def __init__(self, connection):
        self._connection = connection  # _Connection instance
        self._cur = connection._conn.cursor()

    def execute(self, sql, params=()):
        # Another _ReconnectingCursor sharing this same _Connection (e.g. a
        # conn.execute() shorthand call from _bind_rls_identity, or this
        # cursor's own prior retry below) may have swapped self._connection._conn
        # out for a fresh physical connection after a transient drop. self._cur
        # would then still be bound to the old, now-closed connection — using it
        # raises psycopg2.InterfaceError ("cursor already closed"), not
        # OperationalError, so the retry below would never catch it. Detect that
        # staleness and grab a live cursor before executing.
        if self._cur.connection is not self._connection._conn:
            self._cur = self._connection._conn.cursor()
        for attempt in range(2):
            try:
                self._cur.execute(sql, params)
                return self
            except psycopg2.OperationalError:
                if attempt == 1:
                    raise
                try:
                    _get_pool().putconn(self._connection._conn, close=True)
                except Exception:
                    pass
                self._connection._conn = _get_pool().getconn()
                self._cur = self._connection._conn.cursor()
                # The swapped-in connection is a fresh checkout with no RLS
                # identity bound — without this, the rest of the request would
                # silently fall back to the pool's bypass-RLS role after a
                # transient drop mid-request, with no error raised.
                _bind_rls_identity(self._connection)

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _Connection:
    """Thin wrapper adding sqlite3-compatible conn.execute() shorthand to psycopg2."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        c = _ReconnectingCursor(self)
        c.execute(sql, params)
        return c

    def cursor(self):
        return _ReconnectingCursor(self)

    def commit(self):
        self._conn.commit()
        # SET LOCAL / set_config(..., true) are scoped to the transaction that
        # was just closed by this commit — Postgres reverts both immediately,
        # regardless of whether the caller keeps using this same connection
        # for further queries. Re-bind so a handler that commits mid-function
        # (common: insert + commit, then a couple of read-back queries before
        # close()) doesn't silently lose RLS scoping for its own remaining work.
        _bind_rls_identity(self)

    def rollback(self):
        self._conn.rollback()
        _bind_rls_identity(self)

    def close(self):
        if self._conn is None:
            return
        _release(self._conn)
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self.close()


def _bind_rls_identity(conn):
    """
    Runs SET LOCAL ROLE authenticated + set_config('request.jwt.claims', ...)
    on `conn` if current_user_id is bound for this request — a no-op
    otherwise. Both statements are transaction-local and Postgres silently
    reverts them at the end of the current transaction (COMMIT or ROLLBACK),
    so this needs to run at every point a fresh transaction may start under a
    bound identity: initial checkout (get_connection), after a mid-request
    reconnect swaps in a new raw connection (_ReconnectingCursor's retry
    path), and after commit()/rollback() if the caller keeps using the same
    connection (_Connection.commit/rollback above).
    """
    user_id = current_user_id.get()
    if user_id is None:
        return
    conn.execute("SET LOCAL ROLE authenticated")
    conn.execute(
        "SELECT set_config('request.jwt.claims', %s, true)",
        (json.dumps({"sub": user_id}),),
    )


def get_connection():
    for attempt in range(2):
        raw = _get_pool().getconn()
        conn = _Connection(raw)
        try:
            _bind_rls_identity(conn)
            return conn
        except psycopg2.DatabaseError:
            # A connection that went stale server-side while idle in the pool
            # (e.g. Supabase's transaction pooler dropping it) fails on exactly
            # this first bootstrap query after checkout — same failure shape
            # _ReconnectingCursor already retries mid-request. It's dead, not
            # just mid-transaction, so discard it outright rather than
            # returning it to the pool via _release(). Try once more with a
            # fresh connection before giving up.
            try:
                _get_pool().putconn(raw, close=True)
            except Exception:
                pass
            if attempt == 1:
                raise
        except Exception:
            # Non-connection failure (e.g. the connecting role isn't a member
            # of `authenticated`) — retrying won't help; release cleanly and
            # propagate immediately.
            _release(raw)
            raise


def close_pool():
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            type TEXT NOT NULL,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_recurring INTEGER NOT NULL DEFAULT 0,
            user_id TEXT,
            plaid_transaction_id TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            user_id TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL,
            monthly_limit DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (user_id, type)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS monthly_budgets (
            user_id TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL,
            month TEXT NOT NULL,
            monthly_limit DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (user_id, type, month)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expense_types (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            icon TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_default INTEGER DEFAULT 0,
            user_id TEXT,
            macrocategory_id TEXT,
            UNIQUE (user_id, name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS incomes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_recurring INTEGER NOT NULL DEFAULT 0,
            user_id TEXT,
            credit_type TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_rules (
            id TEXT PRIMARY KEY,
            pattern TEXT NOT NULL,
            expense_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            user_id TEXT,
            UNIQUE (user_id, pattern)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS macrocategories (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            color        TEXT NOT NULL DEFAULT '#a0a0a0',
            budget_limit DOUBLE PRECISION,
            user_id      TEXT,
            UNIQUE (user_id, name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id             TEXT PRIMARY KEY,
            goal_type      TEXT NOT NULL,
            name           TEXT NOT NULL,
            target         DOUBLE PRECISION NOT NULL,
            deadline       TEXT,
            created_at     TEXT NOT NULL,
            allocation_pct DOUBLE PRECISION,
            priority       INTEGER,
            paused         INTEGER DEFAULT 0,
            color          TEXT,
            months_target  INTEGER,
            user_id        TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS savings_contributions (
            id         TEXT PRIMARY KEY,
            goal_id    TEXT NOT NULL,
            amount     DOUBLE PRECISION NOT NULL,
            date       TEXT NOT NULL,
            note       TEXT,
            created_at TEXT NOT NULL,
            expense_id TEXT,
            user_id    TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id    TEXT PRIMARY KEY,
            ai_enabled BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)

    for sql in [
        "ALTER TABLE user_settings ADD COLUMN cycle_start_day INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE user_settings ADD COLUMN seen_category_migration_notice BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE expenses ADD COLUMN subcategory TEXT",
        "ALTER TABLE import_rules ADD COLUMN subcategory TEXT",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            # Postgres aborts the whole transaction on a DDL error (e.g. column
            # already exists on restart) — roll back or every later statement
            # in this same init_db() transaction would fail too.
            conn.rollback()

    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_expenses_user_type ON expenses(user_id, type)",
        "CREATE INDEX IF NOT EXISTS idx_expenses_user_subcategory ON expenses(user_id, subcategory)",
        "CREATE INDEX IF NOT EXISTS idx_incomes_user_date ON incomes(user_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_savings_goals_user ON savings_goals(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_contributions_goal ON savings_contributions(goal_id)",
        "CREATE INDEX IF NOT EXISTS idx_contributions_user ON savings_contributions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_import_rules_user ON import_rules(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_macrocategories_user ON macrocategories(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_budgets_user ON budgets(user_id)",
    ]:
        cursor.execute(sql)

    # Row Level Security: a DB-level backstop under every hand-written user_id
    # filter in the routers/agent tools. Supabase auto-grants anon/authenticated
    # full DML on new public tables, and Postgres enforces nothing by default —
    # without this, anyone holding the (publicly-shipped) anon key could read/write
    # any user's rows directly via Supabase's REST API, bypassing this app entirely.
    # The app's own connection (role `postgres`, BYPASSRLS) is unaffected either way;
    # real enforcement for the app's own queries also requires the SET LOCAL ROLE
    # switch in get_connection() below — which needs its own GRANT here, since a
    # GRANT is a precondition Postgres checks before RLS is ever consulted, not
    # something RLS policies substitute for. `anon` deliberately gets nothing:
    # every legitimate access path (this app, or any future direct PostgREST use)
    # authenticates first and only ever needs the `authenticated` role.
    #
    # app_current_user_id() — deliberately NOT Supabase's own auth.uid() — is
    # what every policy below compares user_id against. auth.uid() casts the
    # same claim to ::uuid internally, which would hard-error under DEV_MODE
    # and the test suite's fixed fake ids ("dev-user-00000000-...",
    # "test-user-00000000-...", deliberately not UUID-shaped). Centralized in
    # one named, discoverable function specifically so a future policy can't
    # accidentally reach for auth.uid() out of habit and end up inconsistent
    # with the rest of this schema.
    cursor.execute("""
        CREATE OR REPLACE FUNCTION app_current_user_id() RETURNS text
        LANGUAGE sql STABLE
        AS $$ SELECT current_setting('request.jwt.claims', true)::json ->> 'sub' $$
    """)

    def _enable_rls(table):
        cursor.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        cursor.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO authenticated")
        cursor.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        cursor.execute(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
            f"USING (user_id = app_current_user_id()) "
            f"WITH CHECK (user_id = app_current_user_id())"
        )

    for table in _USER_OWNED_TABLES:
        if table == "transaction_embeddings":
            continue  # handled below, inside the pgvector try/except — see comment there
        _enable_rls(table)

    # Embeddings table for AI semantic search.
    # Wrapped in try/except because this requires the pgvector extension in Supabase.
    # If it hasn't been enabled yet (CREATE EXTENSION IF NOT EXISTS vector), we warn and
    # continue — the rest of the app works fine without it. RLS is enabled here too,
    # inside the same try/except: the table (and therefore ALTER TABLE ... ENABLE ROW
    # LEVEL SECURITY against it) may not exist yet on a fresh project, so it can't run
    # in the unconditional loop above without breaking init_db() on that first boot.
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transaction_embeddings (
                id          TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id   TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                content     TEXT NOT NULL,
                embedding   vector(1536),
                created_at  TEXT NOT NULL,
                UNIQUE (source_type, source_id)
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_user "
            "ON transaction_embeddings (user_id, source_type)"
        )
        _enable_rls("transaction_embeddings")
    except Exception as emb_err:
        import logging
        logging.getLogger("budget_app").warning(
            "transaction_embeddings init failed — run "
            "CREATE EXTENSION IF NOT EXISTS vector in Supabase first. Error: %s",
            str(emb_err),
        )

    conn.commit()
    conn.close()


def get_user_settings(conn, user_id: str) -> dict:
    row = conn.execute(
        "SELECT ai_enabled, cycle_start_day, seen_category_migration_notice FROM user_settings WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    cycle_start_day = row["cycle_start_day"] if row else 1
    period_start, period_end, period_label = cycle_period_for_date(date.today(), cycle_start_day)
    return {
        "ai_enabled": bool(row["ai_enabled"]) if row else True,
        "cycle_start_day": cycle_start_day,
        "seen_category_migration_notice": bool(row["seen_category_migration_notice"]) if row else False,
        "current_period": {
            "period_start": period_start,
            "period_end": period_end,
            "period_label": period_label,
        },
    }


def save_user_settings(conn, user_id: str, ai_enabled: bool, cycle_start_day: int = 1,
                        seen_category_migration_notice: bool = False):
    conn.execute(
        """
        INSERT INTO user_settings (user_id, ai_enabled, cycle_start_day, seen_category_migration_notice)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET ai_enabled = EXCLUDED.ai_enabled, cycle_start_day = EXCLUDED.cycle_start_day,
            seen_category_migration_notice = EXCLUDED.seen_category_migration_notice
        """,
        (user_id, ai_enabled, cycle_start_day, seen_category_migration_notice),
    )
    conn.commit()


def _month_str(year, month):
    total = (year - 1) * 12 + (month - 1)
    y = total // 12 + 1
    m = total % 12 + 1
    return f"{y}-{m:02d}", y, m


def seed_recurring_forward(name: str, amount: float, type_: str, source_month: str, user_id: str):
    # Seed the next two months in ONE idempotent statement: a 2-row VALUES list
    # filtered by WHERE NOT EXISTS (dedup key name+type+YYYY-MM), replacing the
    # old per-month SELECT-then-INSERT loop. `mo` is the target YYYY-MM used only
    # for the dedup; `date` is the YYYY-MM-01 actually stored. Casts on the first
    # VALUES row stop Postgres inferring `unknown` for the text/param columns.
    conn = get_connection()
    cursor = conn.cursor()
    src_y, src_m = map(int, source_month.split('-'))
    rows = []
    for delta in range(1, 3):
        target_str, ty, tm = _month_str(src_y, src_m + delta)
        target_date = f"{ty}-{tm:02d}-01"
        rows.append((str(uuid.uuid4()), name, amount, type_, target_date, target_str, datetime.now().isoformat(), user_id))
    cursor.execute(
        """
        INSERT INTO expenses (id, name, amount, type, date, created_at, is_recurring, user_id)
        SELECT v.id, v.name, v.amount, v.type, v.date, v.created_at, 1, v.user_id
        FROM (VALUES
            (%s::text, %s::text, %s::double precision, %s::text, %s::text, %s::text, %s::text, %s::text),
            (%s, %s, %s, %s, %s, %s, %s, %s)
        ) AS v(id, name, amount, type, date, mo, created_at, user_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM expenses e
            WHERE e.user_id = v.user_id AND e.name = v.name AND e.type = v.type AND LEFT(e.date, 7) = v.mo
        )
        """,
        rows[0] + rows[1],
    )
    conn.commit()
    conn.close()


def apply_recurring_expenses():
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today()

    cursor.execute("SELECT DISTINCT user_id FROM expenses WHERE is_recurring = 1 AND user_id IS NOT NULL")
    user_ids = [row["user_id"] for row in cursor.fetchall()]

    for user_id in user_ids:
        for delta in range(3):
            target_str, ty, tm = _month_str(today.year, today.month + delta)
            target_date = f"{ty}-{tm:02d}-01"
            src_str, _, _ = _month_str(today.year, today.month + delta - 1)

            cursor.execute("""
                SELECT id, name, amount, type, is_recurring FROM expenses
                WHERE is_recurring = 1
                AND user_id = %s
                AND LEFT(date, 7) = %s
                AND (name || '|' || type) NOT IN (
                    SELECT name || '|' || type FROM expenses
                    WHERE user_id = %s AND LEFT(date, 7) = %s
                )
            """, (user_id, src_str, user_id, target_str))

            for row in cursor.fetchall():
                cursor.execute(
                    "INSERT INTO expenses (id, name, amount, type, date, created_at, is_recurring, user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), row["name"], row["amount"], row["type"],
                     target_date, datetime.now().isoformat(), 1, user_id),
                )

    conn.commit()
    conn.close()


def seed_income_recurring_forward(name: str, amount: float, source_month: str, user_id: str, credit_type: str = None):
    # Same one-statement idempotent seed as seed_recurring_forward, minus `type`,
    # carrying credit_type (cast ::text so a NULL doesn't break VALUES inference).
    # Dedup key is name+YYYY-MM only (incomes have no type).
    conn = get_connection()
    cursor = conn.cursor()
    src_y, src_m = map(int, source_month.split('-'))
    rows = []
    for delta in range(1, 3):
        target_str, ty, tm = _month_str(src_y, src_m + delta)
        target_date = f"{ty}-{tm:02d}-01"
        rows.append((str(uuid.uuid4()), name, amount, target_date, target_str, datetime.now().isoformat(), credit_type, user_id))
    cursor.execute(
        """
        INSERT INTO incomes (id, name, amount, date, created_at, is_recurring, credit_type, user_id)
        SELECT v.id, v.name, v.amount, v.date, v.created_at, 1, v.credit_type, v.user_id
        FROM (VALUES
            (%s::text, %s::text, %s::double precision, %s::text, %s::text, %s::text, %s::text, %s::text),
            (%s, %s, %s, %s, %s, %s, %s, %s)
        ) AS v(id, name, amount, date, mo, created_at, credit_type, user_id)
        WHERE NOT EXISTS (
            SELECT 1 FROM incomes i
            WHERE i.user_id = v.user_id AND i.name = v.name AND LEFT(i.date, 7) = v.mo
        )
        """,
        rows[0] + rows[1],
    )
    conn.commit()
    conn.close()


def apply_recurring_incomes():
    conn = get_connection()
    cursor = conn.cursor()
    today = date.today()

    cursor.execute("SELECT DISTINCT user_id FROM incomes WHERE is_recurring = 1 AND user_id IS NOT NULL")
    user_ids = [row["user_id"] for row in cursor.fetchall()]

    for user_id in user_ids:
        for delta in range(3):
            target_str, ty, tm = _month_str(today.year, today.month + delta)
            target_date = f"{ty}-{tm:02d}-01"
            src_str, _, _ = _month_str(today.year, today.month + delta - 1)
            cursor.execute("""
                SELECT id, name, amount, is_recurring, credit_type FROM incomes
                WHERE is_recurring = 1
                AND user_id = %s
                AND LEFT(date, 7) = %s
                AND name NOT IN (
                    SELECT name FROM incomes WHERE user_id = %s AND LEFT(date, 7) = %s
                )
            """, (user_id, src_str, user_id, target_str))
            for row in cursor.fetchall():
                cursor.execute(
                    "INSERT INTO incomes (id, name, amount, date, created_at, is_recurring, user_id, credit_type) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), row["name"], row["amount"], target_date, datetime.now().isoformat(), 1, user_id, row["credit_type"]),
                )

    conn.commit()
    conn.close()


def _outlier_filtered_totals(conn, months_list: list[str], by_type: bool = False, user_id: str = None):
    if not months_list:
        return {}
    placeholders = ",".join(["%s"] * len(months_list))
    cursor = conn.cursor()

    uid_clause = "AND user_id = %s" if user_id else ""
    uid_params = [user_id] if user_id else []

    cursor.execute(
        f"SELECT id, type, amount, LEFT(date, 7) as mo "
        f"FROM expenses WHERE LEFT(date, 7) IN ({placeholders}) {uid_clause}",
        months_list + uid_params,
    )
    rows = cursor.fetchall()

    cat_amounts: dict[str, list[float]] = {}
    for r in rows:
        cat_amounts.setdefault(r["type"], []).append(r["amount"])

    cat_stats: dict[str, tuple[float, float]] = {}
    for t, amounts in cat_amounts.items():
        if len(amounts) < 3:
            continue
        mean = sum(amounts) / len(amounts)
        std = math.sqrt(sum((a - mean) ** 2 for a in amounts) / len(amounts))
        cat_stats[t] = (mean, std)

    cursor.execute(
        f"SELECT credit_type, LEFT(date, 7) as mo, SUM(amount) as total "
        f"FROM incomes WHERE credit_type IS NOT NULL "
        f"AND LEFT(date, 7) IN ({placeholders}) {uid_clause} "
        f"GROUP BY credit_type, mo",
        months_list + uid_params,
    )
    credit_rows = cursor.fetchall()

    if by_type:
        result: dict = {}
        for r in rows:
            stats = cat_stats.get(r["type"])
            if stats and stats[1] > 0 and abs(r["amount"] - stats[0]) / stats[1] >= 1.5:
                continue
            result.setdefault(r["type"], {}).setdefault(r["mo"], 0.0)
            result[r["type"]][r["mo"]] += r["amount"]
        for cr in credit_rows:
            t, mo = cr["credit_type"], cr["mo"]
            if t in result and mo in result[t]:
                result[t][mo] = max(0.0, result[t][mo] - cr["total"])
        return result
    else:
        totals: dict[str, float] = {}
        for r in rows:
            stats = cat_stats.get(r["type"])
            if stats and stats[1] > 0 and abs(r["amount"] - stats[0]) / stats[1] >= 1.5:
                continue
            totals[r["mo"]] = totals.get(r["mo"], 0.0) + r["amount"]
        for cr in credit_rows:
            mo = cr["mo"]
            totals[mo] = max(0.0, totals.get(mo, 0.0) - cr["total"])
        return totals


def get_avg_monthly_expenses(conn, months: int = 3, user_id: str = None) -> float:
    today = date.today()
    past_months = []
    for i in range(months, 0, -1):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12
            y -= 1
        past_months.append(f"{y}-{m:02d}")
    if not past_months:
        return 0.0
    expense_by_month = _outlier_filtered_totals(conn, past_months, by_type=False, user_id=user_id)
    totals = [expense_by_month.get(m, 0.0) for m in past_months]
    return round(sum(totals) / len(totals), 2)


def _month_bounds(month: str):
    """Return (start_date_str, exclusive_end_date_str) for a YYYY-MM month string."""
    y, m = map(int, month.split("-"))
    end_y, end_m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y}-{m:02d}-01", f"{end_y}-{end_m:02d}-01"


def month_start(months_back: int) -> str:
    today = date.today()
    total = today.year * 12 + today.month - 1 - months_back
    return f"{total // 12}-{total % 12 + 1:02d}-01"


def _clamp_day(year: int, month: int, day: int) -> int:
    """Clamp a day-of-month to the last valid day of (year, month), e.g. day=30
    in February -> 28 (or 29 in a leap year) — mirrors how a credit-card
    statement date behaves in short months."""
    return min(day, calendar.monthrange(year, month)[1])


def cycle_period_for_date(ref_date: date, cycle_start_day: int) -> tuple:
    """Return (period_start, period_end_exclusive, period_label) for the
    billing-cycle period containing ref_date, given a user's cycle_start_day
    (1-31; 1 = classic calendar month). period_start/period_end_exclusive are
    'YYYY-MM-DD' strings usable in `date >= period_start AND date < period_end`.

    period_label is 'YYYY-MM', chosen by which month holds the majority of the
    period's days: cycle_start_day 1-15 labels by the start month (e.g. a
    Jun 9-Jul 8 period is mostly June, labels "2026-06"); cycle_start_day 16-31
    labels by the end month (e.g. a Jun 23-Jul 22 period is mostly July, labels
    "2026-07"). At cycle_start_day=1 this reduces to today's exact convention:
    period_start is the 1st of ref_date's month, period_label is that same month."""
    y, m = ref_date.year, ref_date.month
    start_day_this_month = _clamp_day(y, m, cycle_start_day)

    if ref_date.day >= start_day_this_month:
        start_y, start_m = y, m
    else:
        start_y, start_m = (y - 1, 12) if m == 1 else (y, m - 1)

    end_y, end_m = (start_y + 1, 1) if start_m == 12 else (start_y, start_m + 1)
    period_start = f"{start_y}-{start_m:02d}-{_clamp_day(start_y, start_m, cycle_start_day):02d}"
    period_end_exclusive = f"{end_y}-{end_m:02d}-{_clamp_day(end_y, end_m, cycle_start_day):02d}"

    if cycle_start_day >= 16:
        label_date = date.fromisoformat(period_end_exclusive) - timedelta(days=1)
        period_label = f"{label_date.year}-{label_date.month:02d}"
    else:
        period_label = f"{start_y}-{start_m:02d}"

    return period_start, period_end_exclusive, period_label


def cycle_bounds(period_label: str, cycle_start_day: int) -> tuple:
    """Inverse of cycle_period_for_date's label direction: given a period_label
    ('YYYY-MM', as produced by cycle_period_for_date or persisted in
    monthly_budgets.month) and the same cycle_start_day, return (period_start,
    period_end_exclusive). At cycle_start_day=1 this is exactly _month_bounds —
    do not apply the general formula in that case, it's off by one month."""
    if cycle_start_day == 1:
        return _month_bounds(period_label)

    label_y, label_m = map(int, period_label.split("-"))

    if cycle_start_day >= 16:
        # end-month labeling: the label's month is the period's end month
        period_end_exclusive = f"{label_y}-{label_m:02d}-{_clamp_day(label_y, label_m, cycle_start_day):02d}"
        total = label_y * 12 + (label_m - 1) - 1
        start_y, start_m = total // 12, total % 12 + 1
        period_start = f"{start_y}-{start_m:02d}-{_clamp_day(start_y, start_m, cycle_start_day):02d}"
    else:
        # start-month labeling: the label's month is the period's start month
        period_start = f"{label_y}-{label_m:02d}-{_clamp_day(label_y, label_m, cycle_start_day):02d}"
        total = label_y * 12 + (label_m - 1) + 1
        end_y, end_m = total // 12, total % 12 + 1
        period_end_exclusive = f"{end_y}-{end_m:02d}-{_clamp_day(end_y, end_m, cycle_start_day):02d}"

    return period_start, period_end_exclusive


def _fast_totals_by_type(conn, month: str, user_id: str, cycle_start_day: int = 1) -> dict:
    """SQL-aggregate totals per type for a past month — no row scanning in Python."""
    start, end = _month_bounds(month) if cycle_start_day == 1 else cycle_bounds(month, cycle_start_day)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT type, SUM(amount) as total FROM expenses "
        "WHERE user_id = %s AND date >= %s AND date < %s GROUP BY type",
        (user_id, start, end),
    )
    totals = {r["type"]: round(r["total"], 2) for r in cursor.fetchall()}
    cursor.execute(
        "SELECT credit_type as type, SUM(amount) as total FROM incomes "
        "WHERE user_id = %s AND date >= %s AND date < %s AND credit_type IS NOT NULL GROUP BY credit_type",
        (user_id, start, end),
    )
    for r in cursor.fetchall():
        t = r["type"]
        if t in totals:
            totals[t] = round(max(0.0, totals[t] - r["total"]), 2)
    return totals


def _outlier_filtered_totals_by_cycle(conn, periods: list, by_type: bool = False, user_id: str = None) -> dict:
    """Same z-score-outlier-filtering + credit-netting logic as _outlier_filtered_totals,
    but buckets rows into caller-supplied cycle periods (which may span two calendar
    months) instead of grouping by SQL LEFT(date,7). `periods` is a list of
    (period_start, period_end_exclusive, period_label) tuples; issues one range
    query spanning all of them, then buckets rows into periods in Python."""
    if not periods:
        return {}
    range_start = min(p[0] for p in periods)
    range_end = max(p[1] for p in periods)
    cursor = conn.cursor()

    uid_clause = "AND user_id = %s" if user_id else ""
    uid_params = [user_id] if user_id else []

    def label_for(d: str):
        for p_start, p_end, p_label in periods:
            if p_start <= d < p_end:
                return p_label
        return None

    cursor.execute(
        f"SELECT type, amount, date FROM expenses "
        f"WHERE date >= %s AND date < %s {uid_clause}",
        [range_start, range_end] + uid_params,
    )
    rows = [{**r, "mo": label_for(r["date"])} for r in cursor.fetchall()]
    rows = [r for r in rows if r["mo"] is not None]

    cat_amounts: dict[str, list[float]] = {}
    for r in rows:
        cat_amounts.setdefault(r["type"], []).append(r["amount"])

    cat_stats: dict[str, tuple[float, float]] = {}
    for t, amounts in cat_amounts.items():
        if len(amounts) < 3:
            continue
        mean = sum(amounts) / len(amounts)
        std = math.sqrt(sum((a - mean) ** 2 for a in amounts) / len(amounts))
        cat_stats[t] = (mean, std)

    cursor.execute(
        f"SELECT credit_type, amount, date FROM incomes "
        f"WHERE credit_type IS NOT NULL AND date >= %s AND date < %s {uid_clause}",
        [range_start, range_end] + uid_params,
    )
    credit_rows = [{**r, "mo": label_for(r["date"])} for r in cursor.fetchall()]
    credit_totals: dict = {}
    for r in credit_rows:
        if r["mo"] is None:
            continue
        key = (r["credit_type"], r["mo"])
        credit_totals[key] = credit_totals.get(key, 0.0) + r["amount"]

    if by_type:
        result: dict = {}
        for r in rows:
            stats = cat_stats.get(r["type"])
            if stats and stats[1] > 0 and abs(r["amount"] - stats[0]) / stats[1] >= 1.5:
                continue
            result.setdefault(r["type"], {}).setdefault(r["mo"], 0.0)
            result[r["type"]][r["mo"]] += r["amount"]
        for (t, mo), total in credit_totals.items():
            if t in result and mo in result[t]:
                result[t][mo] = max(0.0, result[t][mo] - total)
        return result
    else:
        totals: dict[str, float] = {}
        for r in rows:
            stats = cat_stats.get(r["type"])
            if stats and stats[1] > 0 and abs(r["amount"] - stats[0]) / stats[1] >= 1.5:
                continue
            totals[r["mo"]] = totals.get(r["mo"], 0.0) + r["amount"]
        for (t, mo), total in credit_totals.items():
            totals[mo] = max(0.0, totals.get(mo, 0.0) - total)
        return totals


def compute_budget_pacing(conn, month: str, lookback_months: int = 3, user_id: str = None, cycle_start_day: int = 1) -> list:
    today = date.today()

    if cycle_start_day == 1:
        cur_month = f"{today.year}-{today.month:02d}"

        if month > cur_month:
            return []

        y, m = map(int, month.split("-"))
        days_in_month = calendar.monthrange(y, m)[1]
        is_current = month == cur_month
        days_elapsed = today.day if is_current else days_in_month

        if not is_current:
            month_spending = _fast_totals_by_type(conn, month, user_id)
            return [
                {"type": t, "spent": s, "projected_spend": None,
                 "days_elapsed": days_elapsed, "days_in_month": days_in_month}
                for t, s in month_spending.items()
            ]

        raw_month = _outlier_filtered_totals(conn, [month], by_type=True, user_id=user_id)
        month_spending = {t: round(sum(mo_totals.values()), 2) for t, mo_totals in raw_month.items()}

        past_months = []
        for i in range(lookback_months, 0, -1):
            py, pm = today.year, today.month - i
            while pm <= 0:
                pm += 12
                py -= 1
            past_months.append(f"{py}-{pm:02d}")

        historical_daily: dict[str, float] = {}
        if past_months:
            by_type = _outlier_filtered_totals(conn, past_months, by_type=True, user_id=user_id)
            for t, month_totals in by_type.items():
                total_days = sum(calendar.monthrange(*map(int, mo.split("-")))[1] for mo in past_months)
                total_spent = sum(month_totals.get(mo, 0.0) for mo in past_months)
                historical_daily[t] = total_spent / total_days if total_days > 0 else 0.0

        remaining_days = days_in_month - days_elapsed
    else:
        cur_start, cur_end, cur_label = cycle_period_for_date(today, cycle_start_day)

        if month > cur_label:
            return []

        period_start, period_end = cycle_bounds(month, cycle_start_day)
        days_in_month = (date.fromisoformat(period_end) - date.fromisoformat(period_start)).days
        is_current = month == cur_label
        days_elapsed = (today - date.fromisoformat(period_start)).days + 1 if is_current else days_in_month

        if not is_current:
            month_spending = _fast_totals_by_type(conn, month, user_id, cycle_start_day)
            return [
                {"type": t, "spent": s, "projected_spend": None,
                 "days_elapsed": days_elapsed, "days_in_month": days_in_month}
                for t, s in month_spending.items()
            ]

        cur_period = (period_start, period_end, month)
        raw_month = _outlier_filtered_totals_by_cycle(conn, [cur_period], by_type=True, user_id=user_id)
        month_spending = {t: round(sum(mo_totals.values()), 2) for t, mo_totals in raw_month.items()}

        past_periods = []
        walk_ref = date.fromisoformat(period_start) - timedelta(days=1)
        for _ in range(lookback_months):
            p_start, p_end, p_label = cycle_period_for_date(walk_ref, cycle_start_day)
            past_periods.append((p_start, p_end, p_label))
            walk_ref = date.fromisoformat(p_start) - timedelta(days=1)
        past_periods.reverse()
        past_months = [p[2] for p in past_periods]

        historical_daily: dict[str, float] = {}
        if past_periods:
            by_type = _outlier_filtered_totals_by_cycle(conn, past_periods, by_type=True, user_id=user_id)
            total_days = sum(
                (date.fromisoformat(p_end) - date.fromisoformat(p_start)).days
                for p_start, p_end, _ in past_periods
            )
            for t, month_totals in by_type.items():
                total_spent = sum(month_totals.get(mo, 0.0) for mo in past_months)
                historical_daily[t] = total_spent / total_days if total_days > 0 else 0.0

        remaining_days = days_in_month - days_elapsed

    all_types = set(month_spending.keys()) | set(historical_daily.keys())

    result = []
    for t in all_types:
        spent = month_spending.get(t, 0.0)
        daily_rate = historical_daily.get(t)

        if daily_rate is not None:
            historical_monthly_avg = daily_rate * days_in_month
            raw_projected = spent + daily_rate * remaining_days
            projected = round(min(raw_projected, max(spent, historical_monthly_avg)), 2)
        elif days_elapsed > 0:
            projected = round((spent / days_elapsed) * days_in_month, 2)
        else:
            projected = None

        result.append({
            "type": t,
            "spent": round(spent, 2),
            "projected_spend": projected,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
        })

    return result


def get_outliers_for_agent(conn, user_id, months=3):
    """
    Shared outlier detection consumed by both the /analysis/outliers HTTP endpoint and
    the AI agent's flag_anomalies tool.  Keeping the logic here ensures both callers
    always flag the same transactions — no risk of two independent implementations
    drifting over time.
    """
    import math

    today = date.today()
    month_list = []
    for i in range(months - 1, -1, -1):
        y = today.year
        m = today.month - i
        while m <= 0:
            m += 12
            y -= 1
        month_list.append("{}-{:02d}".format(y, m))

    if not month_list:
        return []

    placeholders = ",".join(["%s"] * len(month_list))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, type, amount, date FROM expenses "
        "WHERE user_id = %s AND LEFT(date, 7) IN (" + placeholders + ") "
        "ORDER BY date DESC",
        [user_id] + month_list,
    )
    expenses = cursor.fetchall()

    by_type = {}
    for e in expenses:
        t = e["type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(e["amount"])

    type_stats = {}
    for t, amounts in by_type.items():
        n = len(amounts)
        mean = sum(amounts) / n
        std = math.sqrt(sum((a - mean) ** 2 for a in amounts) / n) if n > 1 else 0.0
        type_stats[t] = {"mean": mean, "std": std, "n": n}

    outliers = []
    for e in expenses:
        stats = type_stats.get(e["type"])
        if not stats or stats["n"] < 3 or stats["std"] == 0:
            continue
        z = (e["amount"] - stats["mean"]) / stats["std"]
        if z < 1.5:
            continue
        pct_above = round(((e["amount"] - stats["mean"]) / stats["mean"]) * 100) if stats["mean"] > 0 else 0
        outliers.append({
            "id": e["id"],
            "name": e["name"],
            "type": e["type"],
            "amount": round(e["amount"], 2),
            "date": e["date"][:10],
            "category_avg": round(stats["mean"], 2),
            "z_score": round(z, 2),
            "pct_above_avg": int(pct_above),
        })

    def _by_z(item):
        return item["z_score"]

    outliers.sort(key=_by_z, reverse=True)
    return outliers[:15]
