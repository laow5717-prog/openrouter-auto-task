"""
信用卡绑定数据模型
"""

import json


class CardBindingModel:
    def __init__(self, db):
        self.db = db

    def create_batch(self, task_id, cards):
        """批量创建绑定记录，返回 record id 列表"""
        ids = []
        for card in cards:
            display = card['number'][-4:] if len(card.get('number', '')) >= 4 else card.get('number', '????')
            card_json = json.dumps(card)
            cursor = self.db.execute(
                "INSERT INTO card_bindings (task_id, card_display, card_data_json) VALUES (?, ?, ?)",
                (task_id, display, card_json),
            )
            ids.append(cursor.lastrowid)
        return ids

    def mark_success(self, binding_id, email):
        self.db.execute(
            "UPDATE card_bindings SET status='success', bound_to_email=?, attempted_at=datetime('now','localtime') WHERE id=?",
            (email, binding_id),
        )

    def mark_failed(self, binding_id, error=''):
        self.db.execute(
            "UPDATE card_bindings SET status='failed', error=?, attempted_at=datetime('now','localtime') WHERE id=?",
            (error, binding_id),
        )

    def get_pending(self, task_id):
        rows = self.db.fetchall(
            "SELECT * FROM card_bindings WHERE task_id=? AND status='pending' ORDER BY id",
            (task_id,),
        )
        result = []
        for r in rows:
            d = dict(r)
            d['card'] = json.loads(d['card_data_json']) if d['card_data_json'] else {}
            result.append(d)
        return result

    def get_all_by_task(self, task_id):
        rows = self.db.fetchall(
            "SELECT id, task_id, card_display, status, bound_to_email, error, attempted_at FROM card_bindings WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]

    def get_successfully_bound_card_numbers(self):
        """获取所有已成功绑定的卡号（跨所有任务）"""
        rows = self.db.fetchall(
            "SELECT card_data_json FROM card_bindings WHERE status='success' AND card_data_json IS NOT NULL"
        )
        numbers = set()
        for r in rows:
            try:
                card = json.loads(r['card_data_json'])
                if card.get('number'):
                    numbers.add(card['number'])
            except (json.JSONDecodeError, TypeError):
                pass
        return numbers

    def get_paginated_by_task(self, task_id, page=1, page_size=20, status='', keyword=''):
        conditions = ["task_id=?"]
        params = [task_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])

        where = " WHERE " + " AND ".join(conditions)
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_bindings{where}", params
        )
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_by_email(self, email):
        rows = self.db.fetchall(
            "SELECT id, card_display, status, error, attempted_at, card_data_json FROM card_bindings WHERE bound_to_email=? ORDER BY id",
            (email,),
        )
        result = []
        for r in rows:
            d = dict(r)
            card = {}
            if d.get('card_data_json'):
                try:
                    card = json.loads(d['card_data_json'])
                except (json.JSONDecodeError, TypeError):
                    pass
            d['card_number'] = card.get('number', '')
            d['card_holder'] = f"{card.get('first_name', '')} {card.get('last_name', '')}".strip()
            d['expiry_month'] = card.get('expiry_month', '')
            d['expiry_year'] = card.get('expiry_year', '')
            d['cvc'] = card.get('cvc', '')
            d['country'] = card.get('country', '')
            d['address'] = card.get('address', '')
            d['address2'] = card.get('address2', '')
            d['city'] = card.get('city', '')
            d['state'] = card.get('state', '')
            d['zip'] = card.get('zip', '')
            d['company'] = card.get('company', '')
            del d['card_data_json']
            result.append(d)
        return result

    def count_by_emails(self, emails):
        if not emails:
            return {}
        placeholders = ','.join(['?'] * len(emails))
        rows = self.db.fetchall(
            f"SELECT bound_to_email, COUNT(*) as cnt FROM card_bindings WHERE status='success' AND bound_to_email IN ({placeholders}) GROUP BY bound_to_email",
            list(emails),
        )
        return {r['bound_to_email']: r['cnt'] for r in rows}

    def get_all_paginated(self, page=1, page_size=20, status='', keyword='', date_from='', date_to=''):
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if date_from:
            conditions.append("attempted_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("attempted_at <= ?")
            params.append(f"{date_to} 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_bindings{where}", params
        )
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, card_data_json, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_global_summary(self):
        row = self.db.fetchone(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
            FROM card_bindings"""
        )
        return dict(row) if row else {"total": 0, "success": 0, "failed": 0, "pending": 0}

    def get_all_filtered(self, status='', keyword='', date_from='', date_to=''):
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if date_from:
            conditions.append("attempted_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("attempted_at <= ?")
            params.append(f"{date_to} 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, card_data_json, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id DESC",
            params,
        )
        return [dict(r) for r in rows]

    def delete_pending_by_task(self, task_id):
        """删除指定任务的所有 pending 记录，返回删除行数"""
        cursor = self.db.execute(
            "DELETE FROM card_bindings WHERE task_id=? AND status='pending'",
            (task_id,)
        )
        return cursor.rowcount

    def cleanup_stale_pending(self, active_task_id=None):
        """清理属于已完成/停止/僵尸任务的 pending 记录，返回删除行数"""
        if active_task_id is not None:
            cursor = self.db.execute(
                """DELETE FROM card_bindings
                   WHERE status='pending' AND (
                       task_id IN (SELECT id FROM tasks WHERE status IN ('stopped', 'completed'))
                       OR (task_id IN (SELECT id FROM tasks WHERE status='running') AND task_id != ?)
                   )""",
                (active_task_id,)
            )
        else:
            cursor = self.db.execute(
                "DELETE FROM card_bindings WHERE status='pending'"
            )
        return cursor.rowcount

    def get_summary(self, task_id):
        row = self.db.fetchone(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending
            FROM card_bindings WHERE task_id=?""",
            (task_id,),
        )
        return dict(row) if row else {"total": 0, "success": 0, "failed": 0, "pending": 0}
