"""
SQLite 数据库管理模块
提供连接管理、schema 创建、版本迁移
"""

import contextlib
import os
import sys
import sqlite3
import threading

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    login_password TEXT,
    email_password TEXT,
    status TEXT DEFAULT 'registered',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    total_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    config_json TEXT,
    started_at TEXT DEFAULT (datetime('now','localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS card_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    card_display TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    bound_to_email TEXT,
    error TEXT,
    attempted_at TEXT,
    card_data_json TEXT
);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS recharge_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    card_display TEXT,
    amount REAL DEFAULT 10,
    status TEXT DEFAULT 'pending',
    error TEXT,
    api_response TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS card_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'bind',
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS card_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES card_groups(id) ON DELETE CASCADE,
    card_number TEXT NOT NULL,
    expiry_month TEXT NOT NULL,
    expiry_year TEXT NOT NULL,
    cvc TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    country TEXT DEFAULT '',
    address TEXT DEFAULT '',
    address2 TEXT DEFAULT '',
    city TEXT DEFAULT '',
    state TEXT DEFAULT '',
    zip TEXT DEFAULT '',
    company TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(card_number, group_id)
);

CREATE TABLE IF NOT EXISTS valid_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_number TEXT NOT NULL,
    expiry_month TEXT NOT NULL,
    expiry_year TEXT NOT NULL,
    cvc TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    country TEXT DEFAULT '',
    address TEXT DEFAULT '',
    address2 TEXT DEFAULT '',
    city TEXT DEFAULT '',
    state TEXT DEFAULT '',
    zip TEXT DEFAULT '',
    company TEXT DEFAULT '',
    source_type TEXT NOT NULL,
    source_email TEXT DEFAULT '',
    source_group_id INTEGER,
    validated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(card_number, source_type)
);
"""

_SCHEMA_V4 = """
ALTER TABLE card_pool ADD COLUMN status TEXT DEFAULT '';
"""

_SCHEMA_V5 = """
ALTER TABLE accounts ADD COLUMN credits_balance REAL;
ALTER TABLE accounts ADD COLUMN balance_updated_at TEXT;
"""

# 账单支付选卡规则：3DS 临时冷却状态（一卡一账号绑定/次数冷却由 valid_cards/recharge_logs 实时派生，无需落库）
_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS card_payment_state (
    card_number TEXT PRIMARY KEY,
    tds_until   TEXT,
    tds_reason  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 账单支付：「账单已无法在 Stripe 支付」的 24h 冷却状态。该表从未接线（模型有方法、
# 无调用方），已由 V13 删除；此处保留建表语句只为让版本链完整，勿删。
_SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS invoice_payment_state (
    invoice_id      TEXT PRIMARY KEY,
    email           TEXT DEFAULT '',
    unpayable_until TEXT,
    reason          TEXT DEFAULT '',
    pay_url         TEXT DEFAULT '',
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 并行执行：卡领取的原子占位。status 增加 'processing' 中间态，配 worker_id + claimed_at，
# 使多个 worker 能安全消费同一 task 的卡池（详见 WorkerPool / CardBindingModel.claim_batch）。
# claimed_at 供回收线程判定失联 worker（超时重置回 pending）。
_SCHEMA_V8 = """
ALTER TABLE card_bindings ADD COLUMN worker_id TEXT DEFAULT '';
ALTER TABLE card_bindings ADD COLUMN claimed_at TEXT;
CREATE INDEX IF NOT EXISTS idx_cb_status_claimed ON card_bindings(status, claimed_at);
"""

# 账号绑卡数落库。此前绑卡数只编码在 status 文本 'bound_N_cards' 里，靠字符串解析取值；
# 补绑流程现在会在「账号本就已绑卡（登录后弹出待支付弹窗）」这条分支上只核对不补绑，
# 需要一个不依赖 status 语义的权威计数列。cards_checked_at 记录该计数的核对时刻。
_SCHEMA_V9 = """
ALTER TABLE accounts ADD COLUMN bound_card_count INTEGER;
ALTER TABLE accounts ADD COLUMN cards_checked_at TEXT;
"""

# 账号补充列:apikey = opencode API key（明文，读自 /workspace/<wid>/keys 的 Default key，
# apikey_updated_at 记抓取时刻）；email_verify_link = hotmail.xlsx 每行第三段的 ruoanzhu 收信链接。
# 均 additive、可空，不影响既有读写。
_SCHEMA_V10 = """
ALTER TABLE accounts ADD COLUMN apikey TEXT;
ALTER TABLE accounts ADD COLUMN apikey_updated_at TEXT;
ALTER TABLE accounts ADD COLUMN email_verify_link TEXT;
"""

# 代理池:每账号处理时领一个 HTTP 代理出口 IP（反关联）。username/password 为代理认证凭据，
# host/port 为网关地址（i-proxy 同网关不同端口=不同出口 IP）。status='' 可用、'invalid' 剔除；
# assigned_email 预留给「账号固定绑定 IP」模式（当前动态领取不写它）。UNIQUE 去重。
_SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT DEFAULT '',
    password TEXT DEFAULT '',
    status TEXT DEFAULT '',
    assigned_email TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE(host, port, username)
);
"""

# AdsPower 环境映射:一账号一环境。email 作主键是「一账号一环境」的结构性保证,
# 而不是靠调用方自觉;profile_id 加 UNIQUE 防止两条映射指向同一环境——那会绕过
# AccountRegistry 的单实例约束,两个 worker 同时接管一个浏览器。
# proxy_id 是环境创建时绑定的 AdsPower 代理 ID(仅记录,占用真值以 AdsPower 的
# proxy_count 为准)。环境配额稀缺(实测上限 12),last_used_at 供回收时按最久未用排序。
_SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS adspower_profiles (
    email TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL UNIQUE,
    profile_no TEXT DEFAULT '',
    proxy_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    last_used_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

# 删除 invoice_payment_state：V7 建了这张表，但「标记账单无法支付」这条线一直没接完
# ——模型方法齐全却零调用方，表也始终是空的。多平台改造要给各状态表加 platform 维度，
# 留着它只会多一处需要跟着改的死数据。
_SCHEMA_V13 = """
DROP TABLE IF EXISTS invoice_payment_state;
"""

_MIGRATIONS = {
    1: _SCHEMA_V1,
    2: _SCHEMA_V2,
    3: _SCHEMA_V3,
    4: _SCHEMA_V4,
    5: _SCHEMA_V5,
    6: _SCHEMA_V6,
    7: _SCHEMA_V7,
    8: _SCHEMA_V8,
    9: _SCHEMA_V9,
    10: _SCHEMA_V10,
    11: _SCHEMA_V11,
    12: _SCHEMA_V12,
    13: _SCHEMA_V13,
}


class Database:
    """线程安全的 SQLite 数据库封装"""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = self._default_path()

        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._import_txt_if_needed()

    @staticmethod
    def _default_path():
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base, "data", "openrouter_auto.db")

    def _migrate(self):
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        target = max(_MIGRATIONS.keys())
        if current >= target:
            return
        for version in range(current + 1, target + 1):
            sql = _MIGRATIONS[version]
            self._conn.executescript(sql)
        self._conn.execute(f"PRAGMA user_version = {target}")
        self._conn.commit()

    def _import_txt_if_needed(self):
        """首次运行时从 registered_accounts.txt 导入数据"""
        count = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if count > 0:
            return

        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        txt_path = os.path.join(base, "registered_accounts.txt")
        if not os.path.exists(txt_path):
            return

        imported = 0
        try:
            with open(txt_path, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split('----')
                    if len(parts) >= 2:
                        email = parts[0].strip()
                        login_pw = parts[1].strip() if len(parts) > 1 else None
                        ts = parts[2].strip() if len(parts) > 2 else None
                        status = parts[3].strip() if len(parts) > 3 else 'registered'
                        email_pw = parts[4].strip() if len(parts) > 4 else None
                        self._conn.execute(
                            "INSERT OR IGNORE INTO accounts (email, login_password, email_password, status, created_at) VALUES (?, ?, ?, ?, ?)",
                            (email, login_pw, email_pw, status, ts),
                        )
                        imported += 1
            self._conn.commit()

            migrated_path = txt_path + ".migrated"
            os.rename(txt_path, migrated_path)
            print(f"DB: imported {imported} accounts from TXT, renamed to {os.path.basename(migrated_path)}")
        except Exception as e:
            print(f"DB: TXT import failed: {e}")

    def execute(self, sql, params=None):
        with self._lock:
            cursor = self._conn.execute(sql, params or ())
            self._conn.commit()
            return cursor

    @contextlib.contextmanager
    def transaction(self):
        """把多条语句合成一个事务，异常时整体回滚。

        注意：块内**不能**调用 self.execute/fetchone/fetchall —— _lock 不可重入会死锁，
        且 execute 会提前 commit 破坏原子性。直接用 yield 出来的 conn 执行。
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def fetchone(self, sql, params=None):
        with self._lock:
            return self._conn.execute(sql, params or ()).fetchone()

    def fetchall(self, sql, params=None):
        with self._lock:
            return self._conn.execute(sql, params or ()).fetchall()

    def close(self):
        self._conn.close()
