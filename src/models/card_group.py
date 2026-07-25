"""
信用卡分组模型
"""


class CardGroupModel:
    def __init__(self, db):
        self.db = db

    def create(self, name, group_type='payment', description=''):
        # opencode 充值不再区分分组类型，所有分组统一为支付卡（'payment'）。
        # 保留 type 字段仅为兼容旧数据与表结构。
        cursor = self.db.execute(
            "INSERT INTO card_groups (name, type, description) VALUES (?, ?, ?)",
            (name, group_type, description),
        )
        return cursor.lastrowid

    def update(self, group_id, name=None, description=None):
        parts = []
        params = []
        if name is not None:
            parts.append("name=?")
            params.append(name)
        if description is not None:
            parts.append("description=?")
            params.append(description)
        if not parts:
            return
        params.append(group_id)
        self.db.execute(
            f"UPDATE card_groups SET {', '.join(parts)} WHERE id=?", params
        )

    def delete(self, group_id):
        self.db.execute("DELETE FROM card_groups WHERE id=?", (group_id,))

    def get_all(self):
        rows = self.db.fetchall(
            "SELECT g.*, (SELECT COUNT(*) FROM card_pool WHERE group_id=g.id) as card_count "
            "FROM card_groups g ORDER BY g.id DESC"
        )
        return [dict(r) for r in rows]

    def get_by_id(self, group_id):
        row = self.db.fetchone("SELECT * FROM card_groups WHERE id=?", (group_id,))
        return dict(row) if row else None

    def get_by_type(self, group_type):
        rows = self.db.fetchall(
            "SELECT g.*, (SELECT COUNT(*) FROM card_pool WHERE group_id=g.id) as card_count "
            "FROM card_groups g WHERE g.type=? ORDER BY g.id DESC",
            (group_type,),
        )
        return [dict(r) for r in rows]
