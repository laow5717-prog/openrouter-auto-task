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
