"""
信用卡底料池模型
"""

import json

from src.utils import (
    is_card_expired,
    CARD_STATUS_EXPIRED,
    CARD_STATUS_INVALID,
    CARD_STATUS_PAID,
    CARD_STATUS_BOUND,
    CARD_STATUS_UNUSABLE,
    CARD_STATUS_NOT_SELECTABLE,
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

    def move_non_invalid_to_group(self, source_group_ids, target_group_id, bucket='non_invalid'):
        """把源分组里指定桶的卡**移动**到目标分组，按卡号去重。
        bucket='non_invalid'（默认）=有效+未验证；bucket='valid'=仅有效卡。
        返回 {moved, deduped}：moved=移入的去重卡数，deduped=删除的重复行数。
        卡原状态随卡带走（改 group_id）。同号只保留一行入目标组，其余同号命中行删除，
        避免 UNIQUE(card_number, group_id) 冲突。"""
        if not source_group_ids:
            return {'moved': 0, 'deduped': 0}
        if bucket not in ('non_invalid', 'valid'):
            raise ValueError(f"不支持的桶: {bucket}")
        for gid in source_group_ids:
            self.refresh_expired_status(gid)
        ph_status = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        ph_groups = ','.join('?' * len(source_group_ids))
        if bucket == 'valid':
            # 非无效 且 命中全局历史验证卡
            frag = (f"COALESCE(status,'') NOT IN ({ph_status}) "
                    "AND card_number IN (SELECT card_number FROM valid_cards)")
        else:
            frag = f"COALESCE(status,'') NOT IN ({ph_status})"
        rows = self.db.fetchall(
            f"""SELECT id, card_number FROM card_pool
                WHERE group_id IN ({ph_groups})
                  AND {frag}
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

    # 桶 → 可移动性：'non_invalid' 不是 _bucket_where 的桶，单独拼（= 有效 + 未验证）
    MOVABLE_BUCKETS = ('unverified', 'valid', 'non_invalid')

    def move_bucket_to_group(self, source_group_id, target_group_id, bucket, limit):
        """把源分组内指定桶的卡片，按 id 升序最多 limit 张**移动**到已存在的目标分组。

        与 move_non_invalid_to_group 的区别：目标分组已有同卡号时**跳过**（源行保留），
        而不是删除源行 —— 只移动 N 张的语境下删卡会造成意外丢卡。
        返回 {moved, skipped}。
        """
        if bucket not in self.MOVABLE_BUCKETS:
            raise ValueError(f"不支持的桶: {bucket}")
        if limit <= 0:
            return {'moved': 0, 'skipped': 0}

        # 与 count_buckets / delete_invalid_by_group 一致：先刷新过期状态，
        # 否则已过期但未标记的卡会被算进 unverified 桶。
        self.refresh_expired_status(source_group_id)

        if bucket == 'non_invalid':
            ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
            frag, fparams = f"COALESCE(status,'') NOT IN ({ph})", list(CARD_STATUS_UNUSABLE)
        else:
            frag, fparams = self._bucket_where(bucket)

        rows = self.db.fetchall(
            f"""SELECT id, card_number FROM card_pool
                WHERE group_id=? AND {frag}
                ORDER BY id ASC LIMIT ?""",
            [source_group_id] + fparams + [limit],
        )
        # seen 同时挡住"目标组已有"和"本批内同号重复"两种 UNIQUE(card_number, group_id) 冲突
        seen = {r['card_number'] for r in self.db.fetchall(
            "SELECT card_number FROM card_pool WHERE group_id=?", (target_group_id,))}

        moved = 0
        skipped = 0
        with self.db.transaction() as conn:
            for r in rows:
                if r['card_number'] in seen:
                    skipped += 1
                    continue
                conn.execute(
                    "UPDATE card_pool SET group_id=? WHERE id=?", (target_group_id, r['id']))
                seen.add(r['card_number'])
                moved += 1
        return {'moved': moved, 'skipped': skipped}

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
        # 排除的是 NOT_SELECTABLE（无效 + 已绑定），而非仅 UNUSABLE：
        # 已绑定的卡遵循「一卡一账号」不能再选，但它不是无效卡，
        # 界面的无效桶仍只按 UNUSABLE 归类。
        usable = [c for c in cards if c['status'] not in CARD_STATUS_NOT_SELECTABLE]
        unusable = [c for c in cards if c['status'] in CARD_STATUS_NOT_SELECTABLE]
        return usable, unusable

    def refresh_expired_status(self, group_id=None):
        """按当前日期重新判定过期卡并标记为 expired（卡会随时间推移过期，故每次取卡前刷新）。

        不覆盖 paid/invalid/bound 状态：paid 与 bound 是历史事实（卡已被用掉），
        invalid 已是无效态。返回新标记的数量。
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
            if status in (CARD_STATUS_EXPIRED, CARD_STATUS_INVALID, CARD_STATUS_BOUND):
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
        """标记为无效卡（支付被拒等卡自身原因）。

        底层不变式：valid_cards 成员（曾支付/绑定成功过的有效卡）**永不**被标为 invalid。
        它已被证明可用，再次被拒只应进入临时冷却（见 CardPaymentStateModel.set_cooldown），
        而非永久作废。此守卫是所有「标无效」入口的最终收口——即便上层调用方漏判，
        有效卡也不会被误标。与 mark_bound_by_number 保留 paid 的做法同构。
        """
        self.db.execute(
            "UPDATE card_pool SET status=? WHERE card_number=? "
            "AND card_number NOT IN (SELECT card_number FROM valid_cards)",
            (CARD_STATUS_INVALID, card_number),
        )

    def mark_expired_by_number(self, card_number):
        """标记为已过期（有效期已过）"""
        self.mark_status_by_number(card_number, CARD_STATUS_EXPIRED)

    def mark_bound_by_number(self, card_number):
        """标记为已绑定到某账号（一卡一账号），此后不再参与选卡。

        只改写空状态。以下状态一律保留，因为它们的信息量都比「被绑过」更大：
          - invalid / expired：记录着卡不可用及其归因，本就不会被选中
          - paid：证明该卡真实付款成功过，是卡可用性的最强证据

        被保留的 paid 卡不会因此被重复绑定——建任务时
        card_bindings.get_successfully_bound_card_numbers() 那层派生过滤仍然生效。
        """
        keep = tuple(CARD_STATUS_UNUSABLE) + (CARD_STATUS_PAID,)
        ph = ','.join('?' * len(keep))
        self.db.execute(
            f"UPDATE card_pool SET status=? WHERE card_number=? "
            f"AND COALESCE(status,'') NOT IN ({ph})",
            (CARD_STATUS_BOUND, card_number, *keep),
        )

    def delete_card(self, card_id):
        self.db.execute("DELETE FROM card_pool WHERE id=?", (card_id,))

    def delete_by_group(self, group_id):
        cursor = self.db.execute("DELETE FROM card_pool WHERE group_id=?", (group_id,))
        return cursor.rowcount

    def count_by_group(self, group_id):
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM card_pool WHERE group_id=?", (group_id,))
        return row['cnt'] if row else 0

    def find_number_by_last4(self, last4):
        """按末 4 位在整个卡池反查完整卡号；命中 0 张或多张（撞号）时返回 None。
        用于充值记账时把页面上只能读到的后四位还原成完整卡号。"""
        if not last4 or len(last4) != 4:
            return None
        rows = self.db.fetchall(
            "SELECT DISTINCT card_number FROM card_pool WHERE card_number LIKE ?",
            (f'%{last4}',),
        )
        nums = [r['card_number'] for r in rows if r['card_number'].endswith(last4)]
        return nums[0] if len(nums) == 1 else None

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
