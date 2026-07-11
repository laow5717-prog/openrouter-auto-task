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
