"""
充值记录数据模型
"""

import json


class RechargeLogModel:
    def __init__(self, db):
        self.db = db

    def create(self, email, card_display='', amount=10):
        """创建充值记录，返回记录 ID"""
        cursor = self.db.execute(
            "INSERT INTO recharge_logs (email, card_display, amount) VALUES (?, ?, ?)",
            (email, card_display, amount),
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

    def has_today_record(self, email, card_last4=''):
        """检查今日是否已有充值记录（不管成功失败）"""
        conditions = ["email=?", "DATE(created_at)=DATE('now','localtime')"]
        params = [email]
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

    def get_success_card_numbers(self, email):
        """返回该账号所有『成功支付』记录里出现过的不同卡号集合。
        用于统计一个账号已处于支付成功状态的卡数量（上限 20）。"""
        rows = self.db.fetchall(
            "SELECT DISTINCT card_display FROM recharge_logs "
            "WHERE email=? AND status='success' AND card_display IS NOT NULL AND card_display != ''",
            (email,),
        )
        return set(r['card_display'] for r in rows)

    def success_count_since(self, card_number, hours=24):
        """该卡号在最近 hours 小时内的成功支付次数（R2 次数/冷却判定）。"""
        if not card_number:
            return 0
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM recharge_logs "
            "WHERE card_display=? AND status='success' "
            "AND created_at >= datetime('now','localtime',?)",
            (card_number, f'-{int(hours)} hours'),
        )
        return row['cnt'] if row else 0

    def count_success_by_last4(self):
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
            "WHERE status='success' AND card_display IS NOT NULL AND card_display != '' "
            "GROUP BY last4"
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

    def last_success_at(self, card_number):
        """该卡号最近一次成功支付时间（localtime 字符串），无则 None。"""
        if not card_number:
            return None
        row = self.db.fetchone(
            "SELECT MAX(created_at) as ts FROM recharge_logs "
            "WHERE card_display=? AND status='success'",
            (card_number,),
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
