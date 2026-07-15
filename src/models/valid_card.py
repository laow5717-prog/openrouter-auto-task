"""
有效卡记录模型
绑定成功或支付成功的卡会记录到此表
"""


class ValidCardModel:
    def __init__(self, db):
        self.db = db

    def record(self, card_data, source_type, source_email='', source_group_id=None):
        """记录有效卡，source_type: 'bind' 或 'payment'，自动去重"""
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO valid_cards
                   (card_number, expiry_month, expiry_year, cvc,
                    first_name, last_name, country, address, address2, city, state, zip, company,
                    source_type, source_email, source_group_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (card_data.get('number', card_data.get('card_number', '')),
                 card_data.get('expiry_month', ''), card_data.get('expiry_year', ''),
                 card_data.get('cvc', ''),
                 card_data.get('first_name', ''), card_data.get('last_name', ''),
                 card_data.get('country', ''), card_data.get('address', ''),
                 card_data.get('address2', ''), card_data.get('city', ''),
                 card_data.get('state', ''), card_data.get('zip', ''),
                 card_data.get('company', ''),
                 source_type, source_email, source_group_id),
            )
        except Exception:
            pass

    def get_bound_email(self, card_number):
        """该卡首次支付成功绑定的账号（source_type='payment' 的 source_email）。
        因 valid_cards 对 (card_number, source_type) 唯一且 INSERT OR IGNORE，首次写入后不被覆盖，
        故此值即"永久绑定账号"。无记录返回 ''。"""
        if not card_number:
            return ''
        row = self.db.fetchone(
            "SELECT source_email FROM valid_cards "
            "WHERE card_number=? AND source_type='payment' LIMIT 1",
            (card_number,),
        )
        return (row['source_email'] or '') if row else ''

    def get_all_for_export(self, source_type=''):
        """导出用：取全部有效卡（不分页），可按 source_type 过滤。"""
        if source_type:
            rows = self.db.fetchall(
                "SELECT * FROM valid_cards WHERE source_type=? ORDER BY id DESC", (source_type,)
            )
        else:
            rows = self.db.fetchall("SELECT * FROM valid_cards ORDER BY id DESC")
        return [dict(r) for r in rows]

    def get_all(self, page=1, page_size=20, source_type='', keyword=''):
        conditions = []
        params = []
        if source_type:
            conditions.append("source_type=?")
            params.append(source_type)
        if keyword:
            conditions.append("(card_number LIKE ? OR source_email LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
            params.extend([f"%{keyword}%"] * 4)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(f"SELECT COUNT(*) as cnt FROM valid_cards{where}", params)
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT * FROM valid_cards{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_summary(self):
        row = self.db.fetchone(
            """SELECT COUNT(*) as total,
                COALESCE(SUM(CASE WHEN source_type='bind' THEN 1 ELSE 0 END), 0) as bind_count,
                COALESCE(SUM(CASE WHEN source_type='payment' THEN 1 ELSE 0 END), 0) as payment_count
            FROM valid_cards"""
        )
        return dict(row) if row else {"total": 0, "bind_count": 0, "payment_count": 0}

    def is_valid(self, card_number):
        row = self.db.fetchone("SELECT id FROM valid_cards WHERE card_number=? LIMIT 1", (card_number,))
        return row is not None
