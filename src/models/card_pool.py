"""
信用卡底料池模型
"""

import json


class CardPoolModel:
    def __init__(self, db):
        self.db = db

    def find_cards_in_other_groups(self, group_id, card_numbers):
        """检查哪些卡号已存在于其他分组中，返回 {card_number: group_name}"""
        if not card_numbers:
            return {}
        placeholders = ','.join(['?'] * len(card_numbers))
        rows = self.db.fetchall(
            f"""SELECT cp.card_number, cg.name as group_name
                FROM card_pool cp JOIN card_groups cg ON cp.group_id = cg.id
                WHERE cp.group_id != ? AND cp.card_number IN ({placeholders})""",
            [group_id] + list(card_numbers),
        )
        return {r['card_number']: r['group_name'] for r in rows}

    def add_cards(self, group_id, cards):
        """批量添加卡片到分组，按卡号去重（同分组内+跨分组），返回 (added, skipped, conflicts)
        conflicts: 已存在于其他分组的卡号列表 [{number, group_name}]
        """
        # 先检查跨分组冲突
        numbers = [c['number'] for c in cards if c.get('number')]
        conflicts_map = self.find_cards_in_other_groups(group_id, numbers)

        added = 0
        skipped = 0
        conflicts = []
        for card in cards:
            num = card.get('number', '')
            if num in conflicts_map:
                conflicts.append({'number': num, 'group_name': conflicts_map[num]})
                continue
            try:
                self.db.execute(
                    """INSERT INTO card_pool
                       (group_id, card_number, expiry_month, expiry_year, cvc,
                        first_name, last_name, country, address, address2, city, state, zip, company)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (group_id, num, card['expiry_month'], card['expiry_year'], card['cvc'],
                     card['first_name'], card['last_name'], card.get('country', ''),
                     card.get('address', ''), card.get('address2', ''),
                     card.get('city', ''), card.get('state', ''), card.get('zip', ''),
                     card.get('company', '')),
                )
                added += 1
            except Exception:
                skipped += 1
        return added, skipped, conflicts

    def get_by_group(self, group_id, page=1, page_size=20):
        offset = (page - 1) * page_size
        total_row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM card_pool WHERE group_id=?", (group_id,)
        )
        total = total_row['cnt'] if total_row else 0
        rows = self.db.fetchall(
            "SELECT * FROM card_pool WHERE group_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (group_id, page_size, offset),
        )
        return [dict(r) for r in rows], total

    def get_all_by_group(self, group_id):
        """获取分组内所有卡片（用于任务执行）"""
        rows = self.db.fetchall(
            "SELECT * FROM card_pool WHERE group_id=? ORDER BY id", (group_id,)
        )
        return [dict(r) for r in rows]

    def get_cards_as_list(self, group_id):
        """获取分组内所有卡片，转换为原有 card dict 格式"""
        rows = self.get_all_by_group(group_id)
        cards = []
        for r in rows:
            cards.append({
                'number': r['card_number'],
                'expiry_month': r['expiry_month'],
                'expiry_year': r['expiry_year'],
                'cvc': r['cvc'],
                'first_name': r['first_name'],
                'last_name': r['last_name'],
                'country': r.get('country', ''),
                'address': r.get('address', ''),
                'address2': r.get('address2', ''),
                'city': r.get('city', ''),
                'state': r.get('state', ''),
                'zip': r.get('zip', ''),
                'company': r.get('company', ''),
            })
        return cards

    def delete_card(self, card_id):
        self.db.execute("DELETE FROM card_pool WHERE id=?", (card_id,))

    def delete_by_group(self, group_id):
        cursor = self.db.execute("DELETE FROM card_pool WHERE group_id=?", (group_id,))
        return cursor.rowcount

    def count_by_group(self, group_id):
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM card_pool WHERE group_id=?", (group_id,))
        return row['cnt'] if row else 0
