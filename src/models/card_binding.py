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
