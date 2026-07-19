"""
账号数据模型
"""


class AccountModel:
    def __init__(self, db):
        self.db = db

    def upsert(self, email, cf_password=None, email_password=None, status='registered'):
        existing = self.db.fetchone("SELECT id, cf_password, email_password FROM accounts WHERE email = ?", (email,))
        if existing:
            final_pw = cf_password if cf_password else existing['cf_password']
            final_ep = email_password if email_password else existing['email_password']
            self.db.execute(
                "UPDATE accounts SET cf_password=?, email_password=?, status=?, updated_at=datetime('now','localtime') WHERE email=?",
                (final_pw, final_ep, status, email),
            )
        else:
            self.db.execute(
                "INSERT INTO accounts (email, cf_password, email_password, status) VALUES (?, ?, ?, ?)",
                (email, cf_password, email_password, status),
            )

    def update_status(self, email, status):
        self.db.execute(
            "UPDATE accounts SET status=?, updated_at=datetime('now','localtime') WHERE email=?",
            (status, email),
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
