"""平台账号数据模型

一个邮箱在每个平台各有一行，`(platform, email)` 唯一。承载该平台自己的登录密码、
状态、余额、API key 与租户 id——这些都不跨平台共享。

与 AccountModel 的分工：AccountModel 管身份（邮箱凭据 + GitHub 密码 + GitHub 注册
结果），本模型管「这个身份在某平台上的账号」。同一邮箱在 A 平台被封不影响它在
B 平台的状态，这条隔离就落在本表的 UNIQUE(platform, email) 上。

「没有行」是有意义的状态：表示该邮箱尚未在这个平台开通。调用方不要用空状态行
去表达这件事——那样会让「未开通」和「已开通但状态未知」混成一谈。
"""


class PlatformAccountModel:
    def __init__(self, db):
        self.db = db

    # ---------- 写 ----------

    def ensure(self, platform, email, login_password=None, status=None, tenant_id=None):
        """确保 (platform, email) 有一行，并按需更新传入的字段。

        只有显式传入非 None 的字段才会覆盖，省略的字段保持原值——与 AccountModel.upsert
        同语义，避免「只想改状态」的调用把密码清空。
        """
        if not platform or not email:
            return
        row = self.db.fetchone(
            "SELECT id FROM platform_accounts WHERE platform=? AND email=?",
            (platform, email),
        )
        if row is None:
            self.db.execute(
                "INSERT INTO platform_accounts (platform, email, login_password, status, tenant_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (platform, email, login_password, status or '', tenant_id or ''),
            )
            return

        sets, params = [], []
        for column, value in (('login_password', login_password),
                              ('status', status),
                              ('tenant_id', tenant_id)):
            if value is not None:
                sets.append(f"{column}=?")
                params.append(value)
        if not sets:
            return
        sets.append("updated_at=datetime('now','localtime')")
        params.extend([platform, email])
        self.db.execute(
            f"UPDATE platform_accounts SET {', '.join(sets)} WHERE platform=? AND email=?",
            params,
        )

    def update_status(self, platform, email, status):
        """写平台状态。行不存在时自动建行——账号在本平台第一次产生状态即开通。"""
        self.ensure(platform, email, status=status)

    def update_tenant_id(self, platform, email, tenant_id):
        """记录平台侧租户/工作区 id（opencode 的 wrk_xxx）。空值跳过，不覆盖已有值。"""
        if not tenant_id:
            return
        self.ensure(platform, email, tenant_id=tenant_id)

    def update_balance(self, platform, email, balance):
        """记录该平台账号最近一次读到的余额（美元）。"""
        if balance is None:
            return
        self.ensure(platform, email)
        self.db.execute(
            "UPDATE platform_accounts SET credits_balance=?, "
            "balance_updated_at=datetime('now','localtime'), "
            "updated_at=datetime('now','localtime') WHERE platform=? AND email=?",
            (float(balance), platform, email),
        )

    def update_apikey(self, platform, email, apikey):
        """记录该平台抓到的 API key（明文）。空值跳过，避免抓取失败覆盖已有 key。"""
        if not apikey:
            return
        self.ensure(platform, email)
        self.db.execute(
            "UPDATE platform_accounts SET apikey=?, "
            "apikey_updated_at=datetime('now','localtime'), "
            "updated_at=datetime('now','localtime') WHERE platform=? AND email=?",
            (apikey, platform, email),
        )

    def delete_by_emails(self, platform, emails):
        if not emails:
            return 0
        placeholders = ','.join(['?'] * len(emails))
        cur = self.db.execute(
            f"DELETE FROM platform_accounts WHERE platform=? AND email IN ({placeholders})",
            [platform] + list(emails),
        )
        return cur.rowcount

    def delete_all_for_emails(self, emails):
        """删除这些邮箱在**所有**平台的账号行（供删除身份时级联清理）。"""
        if not emails:
            return 0
        placeholders = ','.join(['?'] * len(emails))
        cur = self.db.execute(
            f"DELETE FROM platform_accounts WHERE email IN ({placeholders})", list(emails)
        )
        return cur.rowcount

    # ---------- 读 ----------

    def get(self, platform, email):
        """返回 dict | None。None 表示该邮箱尚未在此平台开通。"""
        row = self.db.fetchone(
            "SELECT * FROM platform_accounts WHERE platform=? AND email=?",
            (platform, email),
        )
        return dict(row) if row else None

    def get_status(self, platform, email):
        """取平台状态；无行返回 ''（未开通与空状态在这里刻意合并，调用方多数只关心是否终态）。"""
        row = self.db.fetchone(
            "SELECT status FROM platform_accounts WHERE platform=? AND email=?",
            (platform, email),
        )
        return (row['status'] or '') if row else ''

    def get_all(self, platform):
        rows = self.db.fetchall(
            "SELECT * FROM platform_accounts WHERE platform=? ORDER BY id", (platform,)
        )
        return [dict(r) for r in rows]

    def map_by_email(self, platform, emails=None):
        """{email: 平台账号 dict}。emails 为空表示取该平台全部。

        供账号列表接口一次性取回平台字段，避免 N+1 查询。
        """
        if emails is None:
            rows = self.db.fetchall(
                "SELECT * FROM platform_accounts WHERE platform=?", (platform,)
            )
        elif not emails:
            return {}
        else:
            placeholders = ','.join(['?'] * len(emails))
            rows = self.db.fetchall(
                f"SELECT * FROM platform_accounts WHERE platform=? AND email IN ({placeholders})",
                [platform] + list(emails),
            )
        return {r['email']: dict(r) for r in rows}

    def statuses_by_email(self, emails=None):
        """{email: {platform: status}}——跨全部平台。

        AdsPower 回收判据要的就是它：只有当一个邮箱在**所有已开通平台**都是终态时，
        它的浏览器环境才可以被回收。
        """
        if emails is None:
            rows = self.db.fetchall("SELECT email, platform, status FROM platform_accounts")
        elif not emails:
            return {}
        else:
            placeholders = ','.join(['?'] * len(emails))
            rows = self.db.fetchall(
                f"SELECT email, platform, status FROM platform_accounts "
                f"WHERE email IN ({placeholders})",
                list(emails),
            )
        out = {}
        for r in rows:
            out.setdefault(r['email'], {})[r['platform']] = r['status'] or ''
        return out

    def count(self, platform=None):
        if platform:
            row = self.db.fetchone(
                "SELECT COUNT(*) AS cnt FROM platform_accounts WHERE platform=?", (platform,)
            )
        else:
            row = self.db.fetchone("SELECT COUNT(*) AS cnt FROM platform_accounts")
        return row['cnt']
