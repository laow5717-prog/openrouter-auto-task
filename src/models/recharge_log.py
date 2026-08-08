"""
充值记录数据模型

每条记录带 platform：它是「这张卡在这个平台上付款成功过没有」的唯一真值来源。
漏掉 platform 过滤的后果很具体：一张在 opencode 成功过的卡，到了新平台会被当成
「好卡」排到队尾，而它在那边其实一次都没试过，本该优先消耗。

**它已经不再决定拒付时判废还是只冷却。** 那套「按 last_success_at 分岔」的逻辑在
连续失败计数（card_payment_state.fail_streak）落地时删掉了：现在拒付一律冷却，
判废只看连续失败次数，好卡的豁免收口在 mark_invalid_by_number 的 valid_cards 守卫。
`last_success_at` / `success_count_since` 因此已无生产调用方，保留只为排障查询。

统计类查询（all_success_card_numbers / last_success_at / success_count_since /
count_success_by_last4 / get_success_card_numbers）一律要求 platform。
create/mark_* 这类按 id 操作的写入不需要——记录建的时候平台就定死了。
"""

import json


class RechargeLogModel:
    def __init__(self, db):
        self.db = db

    def create(self, platform, email, card_display='', amount=10):
        """创建充值记录，返回记录 ID"""
        cursor = self.db.execute(
            "INSERT INTO recharge_logs (platform, email, card_display, amount) VALUES (?, ?, ?, ?)",
            (platform, email, card_display, amount),
        )
        return cursor.lastrowid

    def update_card(self, log_id, card_display):
        self.db.execute(
            "UPDATE recharge_logs SET card_display=? WHERE id=?",
            (card_display, log_id),
        )

    def mark_success(self, log_id, api_response=None):
        self.db.execute(
            "UPDATE recharge_logs SET status='success', api_response=? WHERE id=?",
            (json.dumps(api_response, ensure_ascii=False) if api_response else None, log_id),
        )

    def mark_failed(self, log_id, error='', api_response=None):
        self.db.execute(
            "UPDATE recharge_logs SET status='failed', error=?, api_response=? WHERE id=?",
            (error, json.dumps(api_response, ensure_ascii=False) if api_response else None, log_id),
        )

    def delete(self, log_id):
        """删除一条充值记录。用于撤销「仅执行账单支付、未实际 Top-up」时预建的占位记录，
        避免其被误记为 $10 充值成功。账单支付本身的记账由充值流程内部独立完成。"""
        self.db.execute(
            "DELETE FROM recharge_logs WHERE id=?",
            (log_id,),
        )

    def has_today_record(self, platform, email, card_last4=''):
        """检查该平台今日是否已有充值记录（不管成功失败）"""
        conditions = ["platform=?", "email=?", "DATE(created_at)=DATE('now','localtime')"]
        params = [platform, email]
        if card_last4:
            conditions.append("card_display LIKE ?")
            params.append(f"%{card_last4}")
        row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM recharge_logs WHERE {' AND '.join(conditions)}",
            params,
        )
        return row['cnt'] > 0 if row else False

    def get_by_email(self, email):
        rows = self.db.fetchall(
            "SELECT * FROM recharge_logs WHERE email=? ORDER BY id DESC",
            (email,),
        )
        return [dict(r) for r in rows]

    def success_amount_by_email(self, platform, since):
        """自 since 起，该平台每个账号成功充值的累计金额 {email: float}。

        供 run_daily_pipeline._reusable_recharged 判断「这个账号本次运行已经充进去多少」。
        它是复用闸能收敛的关键：DB 里的 credits_balance 可能是 NULL（balance_after 读不到
        时 update_balance 直接 return），也可能停在旧值不再更新，只靠它判断「未达上限」
        会让账号被一轮轮反复领走、任务永不收敛。本次运行实际充进去的钱每成功一笔就增长，
        与 DB 余额相加即可保证有限步内越过 balance_cap。

        一次聚合而不是逐账号查询——调用方要遍历全部账号，N+1 会让每次领取都打几十条 SQL。

        since 为空返回空 dict：那意味着调用方没拿到运行起始时刻，此时把全时段的历史
        充值算进「本次运行」会让所有账号瞬间看起来都到顶，静默地退化成「一个都不复用」。
        """
        if not since:
            return {}
        rows = self.db.fetchall(
            "SELECT email, SUM(amount) AS total FROM recharge_logs "
            "WHERE platform=? AND status='success' AND created_at >= ? "
            "GROUP BY email",
            (platform, since),
        )
        return {r['email']: float(r['total'] or 0) for r in rows if r['email']}

    def get_success_card_numbers(self, platform, email):
        """返回该账号在此平台所有『成功支付』记录里出现过的不同卡号集合。
        用于统计一个账号已处于支付成功状态的卡数量（上限 20）。"""
        rows = self.db.fetchall(
            "SELECT DISTINCT card_display FROM recharge_logs "
            "WHERE platform=? AND email=? AND status='success' "
            "AND card_display IS NOT NULL AND card_display != ''",
            (platform, email),
        )
        return set(r['card_display'] for r in rows)

    def all_success_card_numbers(self, platform):
        """该平台上「曾成功付款过」的完整卡号集合（用于选卡时区分新卡/好卡）。

        card_display 写入为完整卡号（见 recharge_account._log_card_attempt），去空格后取用；
        历史脱敏串（'•••• 1234'）无法还原完整号，天然落在集合外——最坏结果是把一张好卡
        当成新卡排到队尾，不会把没验证过的卡误判成好卡。

        按平台统计是有意的：跨平台复用的卡在新平台上仍然算「新卡」，排在该平台已验证过的
        好卡之后——它在那里确实还没过过款。
        """
        rows = self.db.fetchall(
            "SELECT DISTINCT replace(card_display,' ','') AS num FROM recharge_logs "
            "WHERE platform=? AND status='success' "
            "AND card_display IS NOT NULL AND card_display != ''",
            (platform,),
        )
        return {r['num'] for r in rows if r['num']}

    def success_count_since(self, platform, card_number, hours=24):
        """该卡号在此平台最近 hours 小时内的成功支付次数（R2 次数/冷却判定）。"""
        if not card_number:
            return 0
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM recharge_logs "
            "WHERE platform=? AND card_display=? AND status='success' "
            "AND created_at >= datetime('now','localtime',?)",
            (platform, card_number, f'-{int(hours)} hours'),
        )
        return row['cnt'] if row else 0

    def count_success_by_last4(self, platform):
        """一次聚合出「每张卡的成功充值次数 / 当日成功充值次数」，返回 {last4: {'total': n, 'today': n}}。

        card_display 的写入格式不统一（可能是完整卡号，也可能是脱敏的 '•••• 1234'，见 app.py
        的 _match_full_card 回退分支），所以这里统一按去空格后的末 4 位聚合，两种格式都能命中。
        列表页按卡号取末 4 位查表即可，避免每张卡一次子查询的 N+1。
        """
        rows = self.db.fetchall(
            "SELECT substr(replace(card_display,' ',''), -4) AS last4, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN DATE(created_at)=DATE('now','localtime') THEN 1 ELSE 0 END) AS today "
            "FROM recharge_logs "
            "WHERE platform=? AND status='success' "
            "AND card_display IS NOT NULL AND card_display != '' "
            "GROUP BY last4",
            (platform,),
        )
        return {
            r['last4']: {'total': r['total'] or 0, 'today': r['today'] or 0}
            for r in rows if r['last4']
        }

    def get_by_card(self, card_number, limit=200):
        """某张卡的全部充值记录（含失败），最近的在前。

        与 count_success_by_last4 保持同一匹配口径——按去空格后的末 4 位，
        以兼容历史上写入格式不一致的 card_display。
        """
        last4 = (card_number or '').replace(' ', '')[-4:]
        if len(last4) != 4:
            return []
        rows = self.db.fetchall(
            "SELECT * FROM recharge_logs "
            "WHERE substr(replace(card_display,' ',''), -4)=? "
            "ORDER BY id DESC LIMIT ?",
            (last4, limit),
        )
        return [dict(r) for r in rows]

    def last_success_at(self, platform, card_number):
        """该卡号在此平台最近一次成功支付时间（localtime 字符串），无则 None。

        这是「拒付时判废还是判冷却」的判据（registration.recharge_account）：本平台
        从未成功过 → 判 invalid；成功过 → 只进 24h 冷却。必须按平台算，否则跨平台
        复用的坏卡在新平台上永远判不了废。
        """
        if not card_number:
            return None
        row = self.db.fetchone(
            "SELECT MAX(created_at) as ts FROM recharge_logs "
            "WHERE platform=? AND card_display=? AND status='success'",
            (platform, card_number),
        )
        return row['ts'] if row and row['ts'] else None

    def get_paginated(self, page=1, page_size=20, email='', status='', date_from='', date_to=''):
        conditions = []
        params = []
        if email:
            conditions.append("email LIKE ?")
            params.append(f"%{email}%")
        if status:
            conditions.append("status=?")
            params.append(status)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("created_at <= ?")
            params.append(f"{date_to} 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM recharge_logs{where}", params
        )
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT * FROM recharge_logs{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    # ------------------------------------------------------------------
    # 报表聚合（只读）
    #
    # 以下方法共用一套口径，任何一处偏离都会让后台的数字对不上账：
    #   1. **金额只算 status='success'**。失败/pending 只进「笔数 / 成功率」，不进金额。
    #   2. **一律按 platform 过滤**（与本文件其余统计方法同规则）。
    #   3. 日期用 `DATE(created_at)` 两侧比较，参数是 'YYYY-MM-DD'。created_at 由
    #      datetime('now','localtime') 写入，与 SQLite 的 DATE('now','localtime') 同时区。
    #   4. **去重卡片数/账号数只在「有卡号的成功记录」子集里算**——见 _distinct_counts
    #      的注释，那里解释了为什么账号数也被 card_display 非空条件裁掉。
    # ------------------------------------------------------------------

    @staticmethod
    def _range_clause(date_from='', date_to=''):
        """把日期区间编译成 (sql_fragment, params)，两端都是闭区间且可单独缺省。

        抽出来是因为下面四个方法都要拼同一段条件，写四遍必然走样——最常见的走样是
        某一处漏了 DATE() 包裹，于是 '2026-08-09' 去和 '2026-08-09 13:20:00' 做字符串
        比较，当天的记录被整段排除，而报表看上去只是「今天没充值」，不会报错。
        """
        sql, params = '', []
        if date_from:
            sql += " AND DATE(created_at) >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND DATE(created_at) <= ?"
            params.append(date_to)
        return sql, params

    def _amount_counts(self, platform, range_sql, range_params):
        """区间内的金额与成功/失败笔数（一条 SQL，用 CASE 分岔）。"""
        row = self.db.fetchone(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status='success' THEN amount ELSE 0 END), 0) AS amount, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count "
            f"FROM recharge_logs WHERE platform=?{range_sql}",
            [platform] + list(range_params),
        )
        return {
            'amount': round(float(row['amount'] or 0), 2) if row else 0.0,
            'success_count': int(row['success_count'] or 0) if row else 0,
            'failed_count': int(row['failed_count'] or 0) if row else 0,
        }

    def _distinct_counts(self, platform, range_sql, range_params):
        """区间内用掉的**不同卡片数**与涉及的**不同账号数**。

        必须与 _amount_counts 分成两条 SQL：COUNT(DISTINCT x) 里塞不进 CASE WHEN
        的成功/失败分岔，硬写成 COUNT(DISTINCT CASE WHEN ... END) 会把 NULL 也算进
        分组、结果偏大且难察觉。这里直接把 status='success' 提到 WHERE 里。

        卡号按**去空格后的完整串**去重，而不是像 count_success_by_last4 那样取末 4 位——
        那个妥协是为兼容历史脱敏串做的，用在「今天用掉几张卡」上会把不同卡号的同末 4 位
        合并成一张，数字偏小。代价是历史脱敏串（'•••• 1234'）会被当成独立一张卡，
        早期日期的卡片数可能偏大；页面上对该列有 title 说明。

        注意 card_display 非空过滤同时裁掉了 account_count：一条没有卡号的 success
        记录属于异常数据（写入路径 _log_card_attempt 总会带卡号），与其为账号数再打一条
        SQL，不如让两个数字口径完全一致——对账时能确定它们看的是同一批行。
        """
        row = self.db.fetchone(
            "SELECT COUNT(DISTINCT replace(card_display,' ','')) AS card_count, "
            "COUNT(DISTINCT email) AS account_count "
            "FROM recharge_logs WHERE platform=? AND status='success' "
            "AND card_display IS NOT NULL AND card_display != ''"
            f"{range_sql}",
            [platform] + list(range_params),
        )
        return {
            'card_count': int(row['card_count'] or 0) if row else 0,
            'account_count': int(row['account_count'] or 0) if row else 0,
        }

    def report_summary(self, platform, date_from='', date_to=''):
        """区间汇总：{'total_amount','success_count','failed_count','card_count','account_count'}。

        区间两端可缺省，缺省即不限该侧（全时段）。
        """
        range_sql, range_params = self._range_clause(date_from, date_to)
        amounts = self._amount_counts(platform, range_sql, range_params)
        distinct = self._distinct_counts(platform, range_sql, range_params)
        return {
            'total_amount': amounts['amount'],
            'success_count': amounts['success_count'],
            'failed_count': amounts['failed_count'],
            **distinct,
        }

    def report_today(self, platform):
        """今日汇总：{'amount','success_count','card_count','account_count'}。

        独立于 report_summary 的区间参数——KPI 卡显示的是「今天」，
        不能因为用户把报表区间拉到上个月就变成 0。
        """
        range_sql = " AND DATE(created_at)=DATE('now','localtime')"
        amounts = self._amount_counts(platform, range_sql, [])
        distinct = self._distinct_counts(platform, range_sql, [])
        return {
            'amount': amounts['amount'],
            'success_count': amounts['success_count'],
            **distinct,
        }

    def report_daily(self, platform, date_from='', date_to=''):
        """逐日明细，日期倒序：
        [{'date','amount','success_count','failed_count','card_count','account_count'}, ...]

        **只返回有记录的日期**，中间没有充值的日子不补零行——补零是画图时的展示决策，
        接口不该凭空造出数据行。

        同样是两条 GROUP BY：金额/笔数一条（含失败行），去重卡数/账号数一条（只看成功行），
        在 Python 里按日期 key 合并。以第一条的日期集合为准，第二条缺失的日期补 0——
        某天全部失败时它确实没有成功卡片，那天该显示 0 张卡而不是从表里消失。
        """
        range_sql, range_params = self._range_clause(date_from, date_to)
        params = [platform] + list(range_params)

        rows = self.db.fetchall(
            "SELECT DATE(created_at) AS date, "
            "COALESCE(SUM(CASE WHEN status='success' THEN amount ELSE 0 END), 0) AS amount, "
            "SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_count "
            f"FROM recharge_logs WHERE platform=?{range_sql} "
            "GROUP BY DATE(created_at) ORDER BY date DESC",
            params,
        )
        distinct_rows = self.db.fetchall(
            "SELECT DATE(created_at) AS date, "
            "COUNT(DISTINCT replace(card_display,' ','')) AS card_count, "
            "COUNT(DISTINCT email) AS account_count "
            "FROM recharge_logs WHERE platform=? AND status='success' "
            "AND card_display IS NOT NULL AND card_display != ''"
            f"{range_sql} "
            "GROUP BY DATE(created_at)",
            params,
        )
        distinct_map = {
            r['date']: {'card_count': int(r['card_count'] or 0),
                        'account_count': int(r['account_count'] or 0)}
            for r in distinct_rows
        }

        result = []
        for r in rows:
            d = r['date']
            counts = distinct_map.get(d) or {'card_count': 0, 'account_count': 0}
            result.append({
                'date': d,
                'amount': round(float(r['amount'] or 0), 2),
                'success_count': int(r['success_count'] or 0),
                'failed_count': int(r['failed_count'] or 0),
                **counts,
            })
        return result

    def report_by_account(self, platform, date_from='', date_to='', limit=0):
        """账号维度榜单，金额倒序：
        [{'email','amount','success_count','card_count','last_at'}, ...]

        只统计成功记录——榜单回答的是「谁充进去了多少」，失败次数在这里没有决策价值。
        card_count 与其余报表方法同口径（去空格后的完整卡号去重）。

        limit=0（默认）不加 LIMIT，返回全部账号。调用方要做「已核销 vs 在用」的拆分，
        必须在**全量**上拆——只对截断后的前 N 行求和，得到的两段金额加起来会小于
        summary.total_amount，而页面上看不出被截断了，是那种会让人对着两个数字
        怀疑人生的错。展示层的截断由调用方自己 slice。
        """
        range_sql, range_params = self._range_clause(date_from, date_to)
        limit_sql = " LIMIT ?" if limit else ""
        rows = self.db.fetchall(
            "SELECT email, "
            "COALESCE(SUM(amount), 0) AS amount, "
            "COUNT(*) AS success_count, "
            "COUNT(DISTINCT replace(card_display,' ','')) AS card_count, "
            "MAX(created_at) AS last_at "
            "FROM recharge_logs WHERE platform=? AND status='success'"
            f"{range_sql} "
            f"GROUP BY email ORDER BY amount DESC{limit_sql}",
            [platform] + list(range_params) + ([int(limit)] if limit else []),
        )
        return [{
            'email': r['email'],
            'amount': round(float(r['amount'] or 0), 2),
            'success_count': int(r['success_count'] or 0),
            'card_count': int(r['card_count'] or 0),
            'last_at': r['last_at'] or '',
        } for r in rows]

    def amount_by_emails(self, platform, emails):
        """这批账号在该平台的成功充值金额 {email: {'today': float, 'total': float}}。

        供账号列表逐行显示「今日充值 / 累计充值」。一次聚合而不是逐账号查询——
        列表页 page_size 可调到 100，N+1 会让每次翻页打上百条 SQL
        （同 card_binding.count_by_emails 的做法）。

        与 success_amount_by_email 的区别：那个按「运行起始时刻」切时段、只出 total，
        服务于复用闸的收敛判断；这个按自然日切、同时出 today 与 total，服务于展示。
        两者口径不同，不要合并。

        emails 为空返回 {}——否则 IN () 是语法错误。
        """
        if not emails:
            return {}
        marks = ','.join('?' * len(emails))
        rows = self.db.fetchall(
            "SELECT email, "
            "COALESCE(SUM(amount), 0) AS total, "
            "COALESCE(SUM(CASE WHEN DATE(created_at)=DATE('now','localtime') "
            "THEN amount ELSE 0 END), 0) AS today "
            f"FROM recharge_logs WHERE platform=? AND status='success' AND email IN ({marks}) "
            "GROUP BY email",
            [platform] + list(emails),
        )
        return {
            r['email']: {'today': round(float(r['today'] or 0), 2),
                         'total': round(float(r['total'] or 0), 2)}
            for r in rows if r['email']
        }
