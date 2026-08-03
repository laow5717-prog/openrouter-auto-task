"""身份数据模型（accounts 表）

本表只装**身份**，不装平台状态：
  - 邮箱身份：email / email_password / email_verify_link
  - GitHub 身份：login_password（就是 GitHub 密码）/ identity_status（GitHub 注册
    与封禁结果：imported / registered / pending / failed / suspended / rejected /
    flagged 等）

平台侧的状态、余额、API key、租户 id 全部在 platform_accounts（见
PlatformAccountModel），按 (platform, email) 隔离。同一邮箱在多个平台跑，
共用本表这一行身份，各自持有一行平台账号。

邮箱与 GitHub 账号当前是严格 1:1（每个 hotmail 邮箱恰好注册一个 GitHub 账号），
所以两者合在一张表里，没有再拆第三张表。将来若要「一邮箱多 GitHub 账号」，
只需拆本表，platform_accounts 不受影响。

`status` 旧列仍在表上但不再被读写——保留它是回滚保险：代码回退到多平台改造前的
版本时，那一列仍是可读的真值。新代码一律用 identity_status。
"""


class AccountModel:
    def __init__(self, db):
        self.db = db

    def upsert(self, email, login_password=None, email_password=None,
               identity_status='registered', email_verify_link=None):
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
                "UPDATE accounts SET login_password=?, email_password=?, identity_status=?, "
                "email_verify_link=?, updated_at=datetime('now','localtime') WHERE email=?",
                (final_pw, final_ep, identity_status, final_link, email),
            )
        else:
            self.db.execute(
                "INSERT INTO accounts (email, login_password, email_password, identity_status, "
                "email_verify_link) VALUES (?, ?, ?, ?, ?)",
                (email, login_password, email_password, identity_status, email_verify_link),
            )

    def update_identity_status(self, email, identity_status):
        self.db.execute(
            "UPDATE accounts SET identity_status=?, updated_at=datetime('now','localtime') "
            "WHERE email=?",
            (identity_status, email),
        )

    def reset_failed_to_registered(self):
        """一次性修正：把被误标 'failed'（实际可用）的账号批量改回 'registered'。

        'failed' 仅由 GitHub 注册失败分支写入，平台流程从不写它；这些账号目前实际
        可用，列表显示"失败"是错误的。返回受影响行数。幂等：无 failed 时返回 0。
        """
        cur = self.db.execute(
            "UPDATE accounts SET identity_status='registered', "
            "updated_at=datetime('now','localtime') WHERE identity_status='failed'"
        )
        return cur.rowcount

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

    def get_paginated(self, page=1, page_size=20, keyword='', identity_status='',
                      date_from='', date_to=''):
        conditions = []
        params = []
        if keyword:
            conditions.append("email LIKE ?")
            params.append(f"%{keyword}%")
        if identity_status:
            conditions.append("identity_status = ?")
            params.append(identity_status)
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
