"""
SQLite 数据库管理模块
提供连接管理、schema 创建、版本迁移
"""

import contextlib
import os
import re
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

# 多平台改造第一步：把 accounts 拆成「身份层」与「平台层」。
#
# accounts 从此只装身份：邮箱（email / email_password / email_verify_link）与 GitHub
# （login_password 实为 GitHub 密码、identity_status 是 GitHub 注册与封禁结果）。这两者
# 当前是严格 1:1——每个 hotmail 邮箱恰好注册一个 GitHub 账号，所以不再拆第三张表；
# 将来若要一邮箱开多个 GitHub 账号，只需再拆 accounts，platform_accounts 不受影响。
#
# platform_accounts 每平台一行，装该平台自己的密码、状态、余额、API key、租户 id。
# login_password 给「用邮箱+密码注册」的平台用；opencode 走 GitHub OAuth，该列留空。
# tenant_id 即 opencode 的 wrk_xxx，泛化为平台侧工作区标识（本次只落库，不改现有获取逻辑）。
#
# status 的归属按层划分：GitHub 注册结果与封禁（imported/pending/suspended/rejected/
# failed/flagged/registered）留身份层；平台业务终态（archived/recharged/subscribed）
# 进平台层。既有数据全部归 platform='opencode'。
# accounts.status 旧列保留不删——代码回退到旧版本时它仍是可读的真值，这是回滚保险。
_SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS platform_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    email TEXT NOT NULL,
    login_password TEXT,
    status TEXT DEFAULT '',
    tenant_id TEXT DEFAULT '',
    credits_balance REAL,
    balance_updated_at TEXT,
    apikey TEXT,
    apikey_updated_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(platform, email)
);

CREATE INDEX IF NOT EXISTS idx_pa_platform_status ON platform_accounts(platform, status);

ALTER TABLE accounts ADD COLUMN identity_status TEXT DEFAULT '';

UPDATE accounts SET identity_status = CASE
    WHEN COALESCE(status,'') IN ('archived','recharged','subscribed') THEN 'registered'
    WHEN COALESCE(status,'') LIKE 'bound_%_cards' THEN 'registered'
    WHEN COALESCE(status,'') IN ('bound','billing_page','interrupted','all_bindings_failed','error') THEN 'registered'
    ELSE COALESCE(status,'')
END;

INSERT OR IGNORE INTO platform_accounts
    (platform, email, status, credits_balance, balance_updated_at,
     apikey, apikey_updated_at, created_at, updated_at)
SELECT 'opencode', email,
       CASE WHEN COALESCE(status,'') IN ('archived','recharged','subscribed')
            THEN status ELSE '' END,
       credits_balance, balance_updated_at, apikey, apikey_updated_at,
       COALESCE(created_at, datetime('now','localtime')),
       datetime('now','localtime')
FROM accounts
WHERE COALESCE(status,'') IN ('archived','recharged','subscribed')
   OR credits_balance IS NOT NULL
   OR COALESCE(apikey,'') != '';
"""

# 卡池平台化第一步：把 card_pool.status 里「每平台一份」的语义搬出去。
#
# 一列 TEXT 装不下多平台状态。拆分口径是「这个状态跟平台有没有关系」：
#   expired —— 有效期已过，与平台无关，**留在 card_pool.status**；
#   bound / invalid / paid —— 都是「这张卡在某个平台上发生过什么」，搬进本表。
# 于是一张卡对某平台的有效状态 = card_pool.status 为 expired 则 expired，
# 否则取 card_platform_state 里 (card_number, platform) 那一行（没有则空）。
#
# 以 card_number 而非 card_pool.id 为键，与既有的 *_by_number 方法一致，也让卡在
# 分组间移动时状态自动跟随。
#
# card_pool.status 列保留不删：迁移后它只承载 expired/''，但代码回退到旧版本时
# refresh_expired_status 仍能按日期重算出 expired，项目照常能跑。
_SCHEMA_V15 = """
CREATE TABLE IF NOT EXISTS card_platform_state (
    card_number TEXT NOT NULL,
    platform    TEXT NOT NULL,
    status      TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (card_number, platform)
);

INSERT OR IGNORE INTO card_platform_state (card_number, platform, status)
SELECT card_number, 'opencode',
       CASE WHEN SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END) > 0 THEN 'invalid'
            WHEN SUM(CASE WHEN status='bound'   THEN 1 ELSE 0 END) > 0 THEN 'bound'
            ELSE 'paid' END
FROM card_pool
WHERE COALESCE(status,'') IN ('bound','invalid','paid')
GROUP BY card_number;

UPDATE card_pool SET status='' WHERE COALESCE(status,'') IN ('bound','invalid','paid');
"""

# 卡池平台化第二步：其余四张状态表加 platform 维度。
#
# valid_cards 与 card_payment_state 的约束里要塞进 platform，而 SQLite 改不了主键和
# UNIQUE，只能重建（create → copy → drop → rename）。既有行全部归 'opencode'。
#
# recharge_logs / card_bindings 加列即可。列默认给 ''、再把既有行 UPDATE 成 'opencode'，
# 而不是直接 DEFAULT 'opencode'——后者会让「调用方忘了传 platform」的新行悄悄变成
# opencode 的数据，那种错误查起来极其痛苦；留空则一眼可见。
_SCHEMA_V16 = """
CREATE TABLE IF NOT EXISTS valid_cards_v16 (
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
    platform TEXT NOT NULL DEFAULT 'opencode',
    validated_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(card_number, source_type, platform)
);

INSERT INTO valid_cards_v16
    (id, card_number, expiry_month, expiry_year, cvc, first_name, last_name,
     country, address, address2, city, state, zip, company,
     source_type, source_email, source_group_id, platform, validated_at)
SELECT id, card_number, expiry_month, expiry_year, cvc, first_name, last_name,
       country, address, address2, city, state, zip, company,
       source_type, source_email, source_group_id, 'opencode', validated_at
FROM valid_cards;

DROP TABLE valid_cards;

ALTER TABLE valid_cards_v16 RENAME TO valid_cards;

CREATE TABLE IF NOT EXISTS card_payment_state_v16 (
    card_number TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT 'opencode',
    tds_until   TEXT,
    tds_reason  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (card_number, platform)
);

INSERT INTO card_payment_state_v16 (card_number, platform, tds_until, tds_reason, updated_at)
SELECT card_number, 'opencode', tds_until, tds_reason, updated_at FROM card_payment_state;

DROP TABLE card_payment_state;

ALTER TABLE card_payment_state_v16 RENAME TO card_payment_state;

ALTER TABLE recharge_logs ADD COLUMN platform TEXT DEFAULT '';

UPDATE recharge_logs SET platform='opencode' WHERE COALESCE(platform,'')='';

ALTER TABLE card_bindings ADD COLUMN platform TEXT DEFAULT '';

UPDATE card_bindings SET platform='opencode' WHERE COALESCE(platform,'')='';

CREATE INDEX IF NOT EXISTS idx_rl_platform_card ON recharge_logs(platform, card_display);

CREATE INDEX IF NOT EXISTS idx_cb_platform_status ON card_bindings(platform, status);
"""

# 连续失败计数：一张卡在某平台连续失败 N 次才判废（此前是首次被拒即永久 invalid）。
#
# 挂在 card_payment_state 而不是 card_pool，理由是主键已经就是 (card_number, platform)
# ——正是这个计数要求的隔离粒度。card_pool.status 只装平台无关的 expired，放不下。
#
# 不从 recharge_logs 派生的理由：那张表里 outcome='unknown' 也会写 status='failed'
# 的一行，而 unknown 按硬约束是「不消耗卡」的，派生就得在 SQL 里去 api_response 的
# JSON 里翻是哪种 failed。显式计数列让判定留在编排层，SQL 保持诚实。
_SCHEMA_V17 = """
ALTER TABLE card_payment_state ADD COLUMN fail_streak INTEGER DEFAULT 0;

ALTER TABLE card_payment_state ADD COLUMN last_fail_at TEXT;
"""

# 运行时可改的配置项。存在理由是分发形态：打包后 config.yaml 落在
# ~/.openrouter-auto-task/ 下，换台机器、换个 AdsPower 账号都得让用户手工找到并编辑
# 一个 YAML 文件。
#
# 做成**通用键值**而不是给 AdsPower 开几个专用列：将来再有配置项要搬到 UI，只是多写
# 一个 key，不用再加一次迁移。value 一律存 TEXT，类型由读取方按 key 自己解释——
# 配置项本来就是异构的，为它们造一套类型系统不值当。
#
# 不回写 config.yaml 是刻意的：那个文件手写、注释密集（config.example.yaml 里每项都有
# 好几行说明），yaml.safe_dump 会把注释全部抹掉。yaml 保持为「默认值」，本表是「覆盖值」。
_SCHEMA_V18 = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""

# 卡池的按分组访问路径。card_pool 此前只有 UNIQUE(card_number, group_id) 的自动索引，
# 它的前导列是 card_number，按 group_id 过滤用不上，于是每次取卡都要全表扫。
# 实测（3.2 万张卡的分组）：取一次可选卡 309ms，而这条路径在每个 worker 每次领卡时
# 都要走一遍，全部压在 Database 那把全局锁上——8 个 worker 因此大半时间在排队等锁，
# 同时只有 4 个浏览器跑得动，连 /api/status 都被挤到超时（2026-08-13 现场）。
_SCHEMA_V19 = """
CREATE INDEX IF NOT EXISTS idx_card_pool_group ON card_pool(group_id);
CREATE INDEX IF NOT EXISTS idx_card_pool_group_status ON card_pool(group_id, status);
CREATE INDEX IF NOT EXISTS idx_rl_platform_status_card
    ON recharge_logs(platform, status, card_display);
"""

# 归档前的身份状态。归档是 identity_status='retired' 就地覆盖，原值当场丢失，
# 于是取消归档只能一律恢复成 'registered'——把一个 banned 账号归档再取消归档，
# 它会「痊愈」成已注册并重新参与任务，而封禁是 GitHub 那边的事实，不会因此改变。
# 存下原值，取消归档时还原回去。
#
# 空串表示「没有归档过」或「归档时是老版本、没留下原值」，后者取消归档时仍回落
# 'registered'——那是旧数据，猜不出原值，保持既有行为比瞎猜好。
_SCHEMA_V20 = """
ALTER TABLE accounts ADD COLUMN identity_status_before_retire TEXT DEFAULT '';
"""

_ADD_COLUMN_RE = re.compile(r'^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)', re.I)

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
    14: _SCHEMA_V14,
    15: _SCHEMA_V15,
    16: _SCHEMA_V16,
    17: _SCHEMA_V17,
    18: _SCHEMA_V18,
    19: _SCHEMA_V19,
    20: _SCHEMA_V20,
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
            self._apply_migration(_MIGRATIONS[version])
        self._conn.execute(f"PRAGMA user_version = {target}")
        self._conn.commit()

    def _apply_migration(self, script):
        """逐语句执行一个迁移脚本，`ADD COLUMN` 先查列是否已存在。

        不用 executescript 是因为它整体执行：脚本里只要有一条 `ADD COLUMN` 撞上
        已存在的列，整个迁移就失败。而这种情况是真会发生的——手工修过库、或在库
        副本上重跑迁移做验证时。跳过已存在的列让迁移可重复执行（幂等），这是在
        生产库副本上预演迁移的前提。
        """
        for stmt in self._split_statements(script):
            m = _ADD_COLUMN_RE.match(stmt)
            if m and self._column_exists(m.group(1), m.group(2)):
                continue
            self._conn.execute(stmt)

    @staticmethod
    def _split_statements(script):
        """按语句边界切分 SQL 脚本。

        用 sqlite3.complete_statement 判定边界，而不是按 ';' 硬切——后者会在
        字符串字面量里含分号时切错。
        """
        statements, buf = [], ''
        for line in script.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                stmt = buf.strip()
                if stmt:
                    statements.append(stmt)
                buf = ''
        tail = buf.strip()
        if tail:
            statements.append(tail)
        return statements

    def _column_exists(self, table, column):
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

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
