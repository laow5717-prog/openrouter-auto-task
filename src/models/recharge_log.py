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
        用于统计一个 Cloudflare 账号已处于支付成功状态的卡数量（上限 20）。"""
        rows = self.db.fetchall(
            "SELECT DISTINCT card_display FROM recharge_logs "
            "WHERE email=? AND status='success' AND card_display IS NOT NULL AND card_display != ''",
            (email,),
        )
        return set(r['card_display'] for r in rows)

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
