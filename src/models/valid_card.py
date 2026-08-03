"""有效卡记录模型

绑定成功或支付成功的卡会记录到此表。**按平台隔离**：`UNIQUE(card_number,
source_type, platform)`——一张卡在 opencode 成功过，不代表它在别的平台也能用，
那边的商户号、风控、发卡行策略都不一样。

这个隔离直接决定两件事：
  - 「有效/未验证」桶怎么分（见 CardPoolModel._bucket_where）；
  - 一张卡被拒时是判废还是只冷却（见 CardPoolModel.mark_invalid_by_number）。
所以本模型的每个查询都必须带 platform，漏掉就会跨平台串数据。
"""


class ValidCardModel:
    def __init__(self, db):
        self.db = db

    def record(self, platform, card_data, source_type, source_email='', source_group_id=None):
        """记录该卡在此平台验证成功，自动去重。source_type: 'bind' 或 'payment'。"""
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO valid_cards
                   (card_number, expiry_month, expiry_year, cvc,
                    first_name, last_name, country, address, address2, city, state, zip, company,
                    source_type, source_email, source_group_id, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (card_data.get('number', card_data.get('card_number', '')),
                 card_data.get('expiry_month', ''), card_data.get('expiry_year', ''),
                 card_data.get('cvc', ''),
                 card_data.get('first_name', ''), card_data.get('last_name', ''),
                 card_data.get('country', ''), card_data.get('address', ''),
                 card_data.get('address2', ''), card_data.get('city', ''),
                 card_data.get('state', ''), card_data.get('zip', ''),
                 card_data.get('company', ''),
                 source_type, source_email, source_group_id, platform),
            )
        except Exception:
            pass

    def get_bound_email(self, platform, card_number):
        """该卡在此平台首次支付成功绑定的账号（source_type='payment' 的 source_email）。

        因 valid_cards 对 (card_number, source_type, platform) 唯一且 INSERT OR IGNORE，
        首次写入后不被覆盖，故此值即「该卡在这个平台上的永久绑定账号」。无记录返回 ''。
        同一张卡在另一个平台可以绑给另一个账号——那是两条独立的记录。
        """
        if not card_number:
            return ''
        row = self.db.fetchone(
            "SELECT source_email FROM valid_cards "
            "WHERE card_number=? AND source_type='payment' AND platform=? LIMIT 1",
            (card_number, platform),
        )
        return (row['source_email'] or '') if row else ''

    def get_all_for_export(self, platform, source_type=''):
        """导出用：取该平台的全部有效卡（不分页），可按 source_type 过滤。"""
        if source_type:
            rows = self.db.fetchall(
                "SELECT * FROM valid_cards WHERE platform=? AND source_type=? ORDER BY id DESC",
                (platform, source_type),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM valid_cards WHERE platform=? ORDER BY id DESC", (platform,)
            )
        return [dict(r) for r in rows]

    def get_all(self, platform, page=1, page_size=20, source_type='', keyword=''):
        conditions = ["platform=?"]
        params = [platform]
        if source_type:
            conditions.append("source_type=?")
            params.append(source_type)
        if keyword:
            conditions.append("(card_number LIKE ? OR source_email LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
            params.extend([f"%{keyword}%"] * 4)

        where = " WHERE " + " AND ".join(conditions)
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(f"SELECT COUNT(*) as cnt FROM valid_cards{where}", params)
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT * FROM valid_cards{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_summary(self, platform):
        row = self.db.fetchone(
            """SELECT COUNT(*) as total,
                COALESCE(SUM(CASE WHEN source_type='bind' THEN 1 ELSE 0 END), 0) as bind_count,
                COALESCE(SUM(CASE WHEN source_type='payment' THEN 1 ELSE 0 END), 0) as payment_count
            FROM valid_cards WHERE platform=?""",
            (platform,),
        )
        return dict(row) if row else {"total": 0, "bind_count": 0, "payment_count": 0}

    def is_valid(self, platform, card_number):
        """该卡是否**在此平台**验证成功过。

        跨平台复用的卡在新平台上返回 False——它在那里还是张没验证过的新卡，
        该判废时就得判废。
        """
        row = self.db.fetchone(
            "SELECT id FROM valid_cards WHERE card_number=? AND platform=? LIMIT 1",
            (card_number, platform),
        )
        return row is not None
