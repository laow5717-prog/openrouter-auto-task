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

    def set_identity_status(self, emails, identity_status, only_from=None):
        """批量改身份状态，返回实际改动行数。

        only_from 非空时加一条 `AND identity_status=?` —— 用于「取消归档」这类
        只该作用于某个来源状态的操作：批量接口误传几个正常账号时，不该把它们的
        状态也一起改掉。

        返回的是 `rowcount`（真实改动数）而不是 `len(emails)`：传进来的邮箱可能不存在，
        或已经是目标状态。前端拿这个数字回显，报虚数会让人以为改了其实没改。
        """
        if not emails:
            return 0
        marks = ','.join('?' * len(emails))
        sql = ("UPDATE accounts SET identity_status=?, "
               "updated_at=datetime('now','localtime') "
               f"WHERE email IN ({marks})")
        params = [identity_status] + list(emails)
        if only_from:
            sql += " AND COALESCE(identity_status,'')=?"
            params.append(only_from)
        return self.db.execute(sql, tuple(params)).rowcount

    def retire(self, emails, retired_status='retired'):
        """归档：把当前身份状态存进 identity_status_before_retire，再置为 retired。

        必须先存后覆盖，且在同一条 UPDATE 里——分两条语句的话，中间崩掉会留下
        一个已经 retired 但没留下原值的账号，之后取消归档就只能瞎猜。

        `WHERE COALESCE(identity_status,'') != retired` 有两个作用：跳过已经归档的行
        （避免把 before_retire 覆盖成 'retired' 自身，那会让原值永久丢失），
        以及让 rowcount 只统计真正发生变化的账号。
        """
        if not emails:
            return 0
        marks = ','.join('?' * len(emails))
        return self.db.execute(
            "UPDATE accounts SET "
            "identity_status_before_retire=COALESCE(identity_status,''), "
            "identity_status=?, updated_at=datetime('now','localtime') "
            f"WHERE email IN ({marks}) AND COALESCE(identity_status,'') != ?",
            tuple([retired_status] + list(emails) + [retired_status]),
        ).rowcount

    def unretire(self, emails, retired_status='retired', fallback='registered'):
        """取消归档：恢复成归档前的身份状态。

        before_retire 为空时回落 fallback——那是 V20 迁移之前归档的老数据，
        原值当初就没被记下来，猜不出来，保持旧行为（一律 registered）。

        恢复后把 before_retire 清空，否则下次归档前它会残留着更早的一次原值，
        看起来像是有效数据。
        """
        if not emails:
            return 0
        marks = ','.join('?' * len(emails))
        return self.db.execute(
            "UPDATE accounts SET "
            "identity_status=CASE "
            "  WHEN COALESCE(identity_status_before_retire,'')='' THEN ? "
            "  ELSE identity_status_before_retire END, "
            "identity_status_before_retire='', "
            "updated_at=datetime('now','localtime') "
            f"WHERE email IN ({marks}) AND COALESCE(identity_status,'')=?",
            tuple([fallback] + list(emails) + [retired_status]),
        ).rowcount

    def get_by_emails(self, emails):
        if not emails:
            return []
        marks = ','.join('?' * len(emails))
        rows = self.db.fetchall(
            f"SELECT * FROM accounts WHERE email IN ({marks}) ORDER BY id", tuple(emails))
        return [dict(r) for r in rows]

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

    def _filter_clause(self, keyword='', identity_status='', identity_statuses=(),
                       exclude_identity_statuses=(), date_from='', date_to=''):
        """把列表筛选条件编译成 (where_sql, params)。

        get_paginated 与 count_by_status_groups 必须用同一份条件：页签上的计数和翻页后
        看到的行数对不上，是那种用户会当成数据错乱来报的 bug。

        identity_status（单值，来自下拉）与 identity_statuses（集合，来自页签）会**同时**
        生效、取交集。不做「谁覆盖谁」的特判：矛盾组合自然得到空列表，这比悄悄忽略
        用户设的某个条件要诚实。

        身份状态一律用 COALESCE(identity_status,'') 参与比较。NULL 在 SQL 三值逻辑里
        既不等于也不不等于任何值，裸列比较会让这些行在 IN 和 NOT IN 里**两边都落空**，
        于是它们从所有页签中消失、而各页签计数之和又对不上总数。
        """
        conditions = []
        params = []
        if keyword:
            conditions.append("email LIKE ?")
            params.append(f"%{keyword}%")
        if identity_status:
            conditions.append("COALESCE(identity_status,'') = ?")
            params.append(identity_status)
        if identity_statuses:
            ph = ','.join(['?'] * len(identity_statuses))
            conditions.append(f"COALESCE(identity_status,'') IN ({ph})")
            params.extend(identity_statuses)
        if exclude_identity_statuses:
            ph = ','.join(['?'] * len(exclude_identity_statuses))
            conditions.append(f"COALESCE(identity_status,'') NOT IN ({ph})")
            params.extend(exclude_identity_statuses)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to + " 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def get_paginated(self, page=1, page_size=20, keyword='', identity_status='',
                      date_from='', date_to='', identity_statuses=(),
                      exclude_identity_statuses=()):
        where, params = self._filter_clause(
            keyword=keyword, identity_status=identity_status,
            identity_statuses=identity_statuses,
            exclude_identity_statuses=exclude_identity_statuses,
            date_from=date_from, date_to=date_to,
        )
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(f"SELECT COUNT(*) as cnt FROM accounts{where}", params)
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT * FROM accounts{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def count_by_status_groups(self, groups, catch_all_key='',
                               keyword='', date_from='', date_to=''):
        """按身份状态分组计数，返回 {'all': n, <组名>: n, ...}。

        groups: {组名: (状态, ...)}；catch_all_key 指定的那一组用「总数 − 其余各组之和」
        倒推，不单独查。这样各页签之和恒等于 all，NULL 或将来新增的未归类状态一定会
        落在兜底组里被看见，而不是从所有页签中蒸发。

        计数只受关键词/日期影响，**不受页签本身影响**——页签要回答「切过去有多少条」，
        带上当前页签的条件去数，每个数字都会等于当前列表长度。

        一条 SQL 用 CASE 分岔，不是每组一次 COUNT：翻一次页就要调它一次。
        """
        where, params = self._filter_clause(
            keyword=keyword, date_from=date_from, date_to=date_to)

        keys = list(groups.keys())
        selects = ["COUNT(*) AS all_cnt"]
        select_params = []
        for i, k in enumerate(keys):
            statuses = tuple(groups[k])
            if not statuses:
                selects.append(f"0 AS g{i}")
                continue
            ph = ','.join(['?'] * len(statuses))
            selects.append(
                f"SUM(CASE WHEN COALESCE(identity_status,'') IN ({ph}) THEN 1 ELSE 0 END) AS g{i}")
            select_params.extend(statuses)

        # SELECT 里的占位符排在 WHERE 之前，参数顺序必须跟着
        row = self.db.fetchone(
            f"SELECT {', '.join(selects)} FROM accounts{where}",
            select_params + params,
        )
        out = {'all': int(row['all_cnt'] or 0) if row else 0}
        for i, k in enumerate(keys):
            out[k] = int(row[f'g{i}'] or 0) if row else 0
        if catch_all_key:
            out[catch_all_key] = out['all'] - sum(out[k] for k in keys)
        return out
