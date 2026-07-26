"""
账号数据模型
"""


class AccountModel:
    def __init__(self, db):
        self.db = db

    def upsert(self, email, login_password=None, email_password=None, status='registered',
               email_verify_link=None):
        existing = self.db.fetchone(
            "SELECT id, login_password, email_password, email_verify_link FROM accounts WHERE email = ?",
            (email,),
        )
        if existing:
            final_pw = login_password if login_password else existing['login_password']
            final_ep = email_password if email_password else existing['email_password']
            # 传入非空才覆盖认证链接，否则保留原值（同 login_password 语义）
            final_link = email_verify_link if email_verify_link else existing['email_verify_link']
            self.db.execute(
                "UPDATE accounts SET login_password=?, email_password=?, status=?, email_verify_link=?, "
                "updated_at=datetime('now','localtime') WHERE email=?",
                (final_pw, final_ep, status, final_link, email),
            )
        else:
            self.db.execute(
                "INSERT INTO accounts (email, login_password, email_password, status, email_verify_link) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, login_password, email_password, status, email_verify_link),
            )

    def update_status(self, email, status):
        self.db.execute(
            "UPDATE accounts SET status=?, updated_at=datetime('now','localtime') WHERE email=?",
            (status, email),
        )

    def reset_failed_to_registered(self):
        """一次性修正：把被误标 'failed'（实际可用）的账号批量改回 'registered'。

        'failed' 仅由 GitHub 注册失败分支写入（web/app._subscribe_one_account），充值流程
        从不写它；这些账号目前实际可用，列表显示"失败"是错误的。返回受影响行数。
        幂等：无 failed 时返回 0。
        """
        cur = self.db.execute(
            "UPDATE accounts SET status='registered', updated_at=datetime('now','localtime') "
            "WHERE status='failed'"
        )
        return cur.rowcount

    def update_bound_cards(self, email, count, sync_status=True):
        """记录账单页核对到的真实已绑卡数。

        count 是页面读数（get_bound_card_count），不是数据库里 card_bindings 的成功条数——
        账号可能绑过卡池之外的卡，那些卡在库里查不到也不做关联，只体现在这个计数上。
        sync_status=True 时顺带把 status 写成 'bound_N_cards' 保持与旧筛选逻辑兼容；
        count 为 0 时不改 status，避免把 registered/failed 等状态覆盖成 'bound_0_cards'。
        """
        if count is None:
            return
        count = int(count)
        if sync_status and count > 0:
            self.db.execute(
                "UPDATE accounts SET bound_card_count=?, cards_checked_at=datetime('now','localtime'), "
                "status=?, updated_at=datetime('now','localtime') WHERE email=?",
                (count, f"bound_{count}_cards", email),
            )
        else:
            self.db.execute(
                "UPDATE accounts SET bound_card_count=?, cards_checked_at=datetime('now','localtime'), "
                "updated_at=datetime('now','localtime') WHERE email=?",
                (count, email),
            )

    def update_balance(self, email, balance):
        """记录该账号最近一次读到的 AI Credits 余额（美元）"""
        if balance is None:
            return
        self.db.execute(
            "UPDATE accounts SET credits_balance=?, balance_updated_at=datetime('now','localtime'), "
            "updated_at=datetime('now','localtime') WHERE email=?",
            (float(balance), email),
        )

    def update_apikey(self, email, apikey):
        """记录该账号从 opencode /keys 页抓到的 API key（明文）。

        apikey 为空/None 时直接跳过，避免把抓取失败写成空值覆盖已有 key。
        """
        if not apikey:
            return
        self.db.execute(
            "UPDATE accounts SET apikey=?, apikey_updated_at=datetime('now','localtime'), "
            "updated_at=datetime('now','localtime') WHERE email=?",
            (apikey, email),
        )

    def backfill_email_verify_link(self, email, link):
        """回填邮箱认证链接（hotmail.xlsx 的 ruoanzhu 收信链接）。

        只写「账号已存在且当前为空」的行——不新建账号、不覆盖已有链接，可重复执行。
        返回受影响行数（1 表示回填成功，0 表示账号不存在或已有链接）。
        """
        if not link:
            return 0
        cur = self.db.execute(
            "UPDATE accounts SET email_verify_link=?, updated_at=datetime('now','localtime') "
            "WHERE email=? AND (email_verify_link IS NULL OR email_verify_link='')",
            (link, email),
        )
        return cur.rowcount

    def get_email_password(self, email):
        """取该账号的邮箱密码（用于登录二次验证时换 mail.tm token）。

        返回 str | None；账号不存在或未存密码均返回 None。
        """
        row = self.db.fetchone(
            "SELECT email_password FROM accounts WHERE email=?", (email,)
        )
        return (dict(row).get('email_password') or None) if row else None

    def get_all(self, order_desc=True):
        order = "DESC" if order_desc else "ASC"
        rows = self.db.fetchall(f"SELECT * FROM accounts ORDER BY id {order}")
        return [dict(r) for r in rows]

    def search(self, term):
        rows = self.db.fetchall(
            "SELECT * FROM accounts WHERE email LIKE ? ORDER BY id DESC",
            (f"%{term}%",),
        )
        return [dict(r) for r in rows]

    def count(self):
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM accounts")
        return row['cnt']

    def delete_by_emails(self, emails):
        if not emails:
            return 0
        placeholders = ','.join(['?'] * len(emails))
        self.db.execute(f"DELETE FROM accounts WHERE email IN ({placeholders})", emails)
        return len(emails)

    def get_paginated(self, page=1, page_size=20, keyword='', status='', date_from='', date_to=''):
        conditions = []
        params = []
        if keyword:
            conditions.append("email LIKE ?")
            params.append(f"%{keyword}%")
        if status:
            conditions.append("status LIKE ?")
            params.append(f"%{status}%")
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to + " 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(f"SELECT COUNT(*) as cnt FROM accounts{where}", params)
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT * FROM accounts{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total
