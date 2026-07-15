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

    def _bucket_where(self, bucket):
        """按状态桶返回 (where 片段, 参数列表)（不含 group 条件）。
        无效=status∈unusable；有效=非无效且在 valid_cards；未验证=非无效且不在 valid_cards。"""
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        if bucket == 'invalid':
            return f"COALESCE(status,'') IN ({ph})", list(CARD_STATUS_UNUSABLE)
        if bucket == 'valid':
            return (f"COALESCE(status,'') NOT IN ({ph}) "
                    "AND card_number IN (SELECT card_number FROM valid_cards)",
                    list(CARD_STATUS_UNUSABLE))
        if bucket == 'unverified':
            return (f"COALESCE(status,'') NOT IN ({ph}) "
                    "AND card_number NOT IN (SELECT card_number FROM valid_cards)",
                    list(CARD_STATUS_UNUSABLE))
        return "", []

    def get_by_group(self, group_id, page=1, page_size=20, bucket=''):
        offset = (page - 1) * page_size
        frag, fparams = self._bucket_where(bucket)
        where = "WHERE group_id=?" + (f" AND {frag}" if frag else "")
        params = [group_id] + fparams
        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_pool {where}", params
        )
        total = total_row['cnt'] if total_row else 0
        rows = self.db.fetchall(
            f"SELECT * FROM card_pool {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def count_buckets(self, group_id):
        """返回分组内各桶数量 {total, invalid, valid, unverified}。统计前刷新过期状态。"""
        self.refresh_expired_status(group_id)
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        u = list(CARD_STATUS_UNUSABLE)
        row = self.db.fetchone(
            f"""SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN COALESCE(status,'') IN ({ph}) THEN 1 ELSE 0 END) AS invalid,
                SUM(CASE WHEN COALESCE(status,'') NOT IN ({ph})
                         AND card_number IN (SELECT card_number FROM valid_cards)
                    THEN 1 ELSE 0 END) AS valid,
                SUM(CASE WHEN COALESCE(status,'') NOT IN ({ph})
                         AND card_number NOT IN (SELECT card_number FROM valid_cards)
                    THEN 1 ELSE 0 END) AS unverified
              FROM card_pool WHERE group_id=?""",
            (*u, *u, *u, group_id),
        )
        if not row:
            return {'total': 0, 'invalid': 0, 'valid': 0, 'unverified': 0}
        return {
            'total': row['total'] or 0,
            'invalid': row['invalid'] or 0,
            'valid': row['valid'] or 0,
            'unverified': row['unverified'] or 0,
        }

    def delete_invalid_by_group(self, group_id):
        """删除分组内所有无效卡（status∈unusable=expired/invalid）。删前刷新过期状态。返回删除数。"""
        self.refresh_expired_status(group_id)
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        cursor = self.db.execute(
            f"DELETE FROM card_pool WHERE group_id=? AND COALESCE(status,'') IN ({ph})",
            (group_id, *CARD_STATUS_UNUSABLE),
        )
        return cursor.rowcount

    def move_non_invalid_to_group(self, source_group_ids, target_group_id):
        """把源分组里所有"非无效"卡（有效+未验证）**移动**到目标分组，按卡号去重。
        返回 {moved, deduped}：moved=移入的去重卡数，deduped=删除的重复行数。
        卡原状态随卡带走（改 group_id）。同号只保留一行入目标组，其余同号非无效行删除，
        避免 UNIQUE(card_number, group_id) 冲突。"""
        if not source_group_ids:
            return {'moved': 0, 'deduped': 0}
        for gid in source_group_ids:
            self.refresh_expired_status(gid)
        ph_status = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        ph_groups = ','.join('?' * len(source_group_ids))
        rows = self.db.fetchall(
            f"""SELECT id, card_number FROM card_pool
                WHERE group_id IN ({ph_groups})
                  AND COALESCE(status,'') NOT IN ({ph_status})
                ORDER BY id""",
            (*source_group_ids, *CARD_STATUS_UNUSABLE),
        )
        existing = {r['card_number'] for r in self.db.fetchall(
            "SELECT card_number FROM card_pool WHERE group_id=?", (target_group_id,))}
        seen = set(existing)
        moved = 0
        deduped = 0
        for r in rows:
            num = r['card_number']
            if num in seen:
                self.db.execute("DELETE FROM card_pool WHERE id=?", (r['id'],))
                deduped += 1
            else:
                self.db.execute(
                    "UPDATE card_pool SET group_id=? WHERE id=?", (target_group_id, r['id']))
                seen.add(num)
                moved += 1
        return {'moved': moved, 'deduped': deduped}

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

    def get_locations_by_number(self, card_number):
        """返回该卡号当前在卡池中的位置列表：[{group_id, group_name, status}]。
        通常 0 或 1 条（add_cards 阻止跨组同号）；用于有效卡弹窗展示"池内分组/状态"。"""
        if not card_number:
            return []
        rows = self.db.fetchall(
            "SELECT p.group_id, g.name AS group_name, COALESCE(p.status,'') AS status "
            "FROM card_pool p LEFT JOIN card_groups g ON g.id=p.group_id "
            "WHERE p.card_number=?",
            (card_number,),
        )
        return [dict(r) for r in rows]
