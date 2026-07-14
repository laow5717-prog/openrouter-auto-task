"""
信用卡底料池模型
"""

import json

from src.utils import (
    is_card_expired,
    CARD_STATUS_EXPIRED,
    CARD_STATUS_INVALID,
    CARD_STATUS_UNUSABLE,
)


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
            status = (CARD_STATUS_EXPIRED
                      if is_card_expired(card.get('expiry_month'), card.get('expiry_year'))
                      else '')
            try:
                self.db.execute(
                    """INSERT INTO card_pool
                       (group_id, card_number, expiry_month, expiry_year, cvc,
                        first_name, last_name, country, address, address2, city, state, zip, company,
                        status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (group_id, num, card['expiry_month'], card['expiry_year'], card['cvc'],
                     card['first_name'], card['last_name'], card.get('country', ''),
                     card.get('address', ''), card.get('address2', ''),
                     card.get('city', ''), card.get('state', ''), card.get('zip', ''),
                     card.get('company', ''), status),
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
                'id': r['id'],
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
                'status': (r['status'] if 'status' in r.keys() else '') or '',
            })
        return cards

    def get_usable_cards_as_list(self, group_id):
        """获取分组内可用于任务的卡片：先按当前日期刷新过期状态，再剔除无效卡（过期/被拒）。

        返回 (usable_cards, unusable_cards)，两者都是 get_cards_as_list 的 dict 格式。
        """
        self.refresh_expired_status(group_id)
        cards = self.get_cards_as_list(group_id)
        usable = [c for c in cards if c['status'] not in CARD_STATUS_UNUSABLE]
        unusable = [c for c in cards if c['status'] in CARD_STATUS_UNUSABLE]
        return usable, unusable

    def refresh_expired_status(self, group_id=None):
        """按当前日期重新判定过期卡并标记为 expired（卡会随时间推移过期，故每次取卡前刷新）。

        不覆盖 paid/invalid 状态：paid 是历史事实，invalid 已是无效态。返回新标记的数量。
        """
        if group_id is None:
            rows = self.db.fetchall("SELECT id, expiry_month, expiry_year, status FROM card_pool")
        else:
            rows = self.db.fetchall(
                "SELECT id, expiry_month, expiry_year, status FROM card_pool WHERE group_id=?",
                (group_id,),
            )
        marked = 0
        for r in rows:
            status = (r['status'] or '')
            if status in (CARD_STATUS_EXPIRED, CARD_STATUS_INVALID):
                continue
            if is_card_expired(r['expiry_month'], r['expiry_year']):
                self.db.execute(
                    "UPDATE card_pool SET status=? WHERE id=?", (CARD_STATUS_EXPIRED, r['id'])
                )
                marked += 1
        return marked

    def mark_status_by_number(self, card_number, status):
        """按卡号标记底料卡状态（如支付成功后标为 'paid'）"""
        self.db.execute(
            "UPDATE card_pool SET status=? WHERE card_number=?",
            (status, card_number),
        )

    def mark_invalid_by_number(self, card_number):
        """标记为无效卡（支付被拒等卡自身原因）"""
        self.mark_status_by_number(card_number, CARD_STATUS_INVALID)

    def mark_expired_by_number(self, card_number):
        """标记为已过期（有效期已过）"""
        self.mark_status_by_number(card_number, CARD_STATUS_EXPIRED)

    def delete_card(self, card_id):
        self.db.execute("DELETE FROM card_pool WHERE id=?", (card_id,))

    def delete_by_group(self, group_id):
        cursor = self.db.execute("DELETE FROM card_pool WHERE group_id=?", (group_id,))
        return cursor.rowcount

    def count_by_group(self, group_id):
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM card_pool WHERE group_id=?", (group_id,))
        return row['cnt'] if row else 0
