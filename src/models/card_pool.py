"""信用卡底料池模型

卡的状态分两层存：

  card_pool.status          只装 `expired`（有效期已过，与平台无关）
  card_platform_state       装 `bound` / `invalid` / `paid`，按 (card_number, platform)

于是「一张卡对某平台的有效状态」= card_pool 说过期就是过期，否则取该平台那一行。
这条口径由 `_EFF_STATUS` 这段 SQL 表达，本模块所有查询共用它。

拆两层的理由：这三个状态说的都是「这张卡在某个平台上发生过什么」——在 opencode 被
绑给某账号、被 Stripe 拒付判废、成功付过款——换个平台（换个商户号、换套风控）
统统不成立。过期则相反，是卡自己的属性，所有平台一致。

因此本模块几乎每个方法都要 platform。少传一个平台参数的后果不是报错，而是安静地
串平台，所以这里不给默认值。
"""

import threading
import time

from src.utils import (
    is_card_expired,
    CARD_STATUS_EXPIRED,
    CARD_STATUS_INVALID,
    CARD_STATUS_PAID,
    CARD_STATUS_BOUND,
    CARD_STATUS_UNUSABLE,
    CARD_STATUS_NOT_SELECTABLE,
)

# 卡对某平台的有效状态。card_pool.status 迁移后只可能是 'expired' 或 ''，
# 所以 expired 优先、其余落到平台状态表。
_EFF_STATUS = "COALESCE(NULLIF(cp.status,''), NULLIF(cps.status,''), '')"

# 平台状态表的 LEFT JOIN。必须 LEFT——绝大多数卡在某平台上没有任何状态记录，
# INNER JOIN 会把它们整批漏掉，表现为「卡池明明有卡却说无可选卡」。
# 用到它的查询，参数列表里 platform 排在最前（JOIN 的 ? 先于 WHERE 的 ?）。
_JOIN_CPS = ("LEFT JOIN card_platform_state cps "
             "ON cps.card_number = cp.card_number AND cps.platform = ?")

# card_pool 除 status 外的全部列。带 JOIN 的查询必须显式列出它们、不能写 `cp.*`：
# cp.* 会带上 card_pool 自己的 status 列，与我们算出来的同名别名撞车，而 sqlite3.Row
# 在列名重复时只认第一个——于是平台状态被静默丢弃，卡明明标了 invalid 却照样出现在
# 可选集里。这个坑排查起来毫无线索（SQL 没报错、数据也确实写进去了）。
_CP_COLS = ("cp.id, cp.group_id, cp.card_number, cp.expiry_month, cp.expiry_year, cp.cvc, "
            "cp.first_name, cp.last_name, cp.country, cp.address, cp.address2, cp.city, "
            "cp.state, cp.zip, cp.company, cp.created_at")


class CardPoolModel:
    # 过期刷新的节流窗口。卡的过期状态按**天**变化，秒级重复扫描毫无意义，
    # 而它在 8 个 worker 的取卡热路径上、又握着全局 DB 锁（见 refresh_expired_status）。
    _EXPIRY_REFRESH_TTL_SEC = 300

    # group_id -> 上次真正扫描的 monotonic 时刻。**类级**：models 会在多处各建一个
    # CardPoolModel 实例（build_models 每次调用都新建一份），实例级的话节流形同虚设。
    _expiry_refreshed_at = {}

    # 可选卡集合的短缓存：(platform, group_id) -> (monotonic, usable, unusable)。
    #
    # get_usable_cards_as_list 要把整个分组的行拉进 Python 转成 dict——3.2 万张卡的分组
    # 实测 210ms，而它在 8 个 worker 的取卡热路径上、握着全局 DB 锁（2026-08-13 现场：
    # 大半时间在等锁，8 个 worker 只跑得动 4 个浏览器）。
    #
    # 缓存安全性靠两条：TTL 只有几秒；且任何改变卡可选性的写操作都会立刻清掉它
    # （见 _invalidate_usable_cache 的调用点）。即便真读到几秒前的快照，下游也还有兜底
    # ——registration 在用卡前会**实时复查冷却**，payment_registry 另有跨 worker 排他。
    _USABLE_CACHE_TTL_SEC = 5.0
    _usable_cache = {}
    _usable_cache_lock = threading.Lock()

    def __init__(self, db):
        self.db = db

    @classmethod
    def _invalidate_usable_cache(cls):
        """任何改变「哪些卡可选」的写操作之后都要调。

        整体清空而不是按 key 清：一次写最多影响一两个 (platform, group) 组合，而缓存
        本来就只有几个条目、重建也只要一次查询；按 key 精细失效反而容易漏——漏一个
        就是拿着过期快照反复去刷一张已经判废的卡。
        """
        with cls._usable_cache_lock:
            cls._usable_cache.clear()

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

        入库只判过期（平台无关），不写任何平台状态——新卡对每个平台都是全新的。
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
        if added:
            self._invalidate_usable_cache()
        return added, skipped, conflicts

    # ---------- 桶（界面分类） ----------

    def _bucket_where(self, bucket, platform):
        """按状态桶返回 (where 片段, 参数列表)（不含 group 条件）。

        无效 = 该平台视角下状态 ∈ unusable；
        有效 = 非无效 且 **在本平台**曾验证成功过（valid_cards 里有本平台的记录）；
        未验证 = 非无效 且 本平台没验证记录。

        「在本平台」这个限定是多平台隔离的核心。valid_cards 的成员身份此前被当成全局
        不变式，一张卡在 opencode 成功过就在所有视图里算有效卡——那会让它在新平台
        既进不了未验证桶、又永远标不成 invalid。
        """
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        if bucket == 'invalid':
            return f"{_EFF_STATUS} IN ({ph})", list(CARD_STATUS_UNUSABLE)
        if bucket == 'valid':
            return (f"{_EFF_STATUS} NOT IN ({ph}) "
                    "AND cp.card_number IN "
                    "    (SELECT card_number FROM valid_cards WHERE platform=?)",
                    list(CARD_STATUS_UNUSABLE) + [platform])
        if bucket == 'unverified':
            return (f"{_EFF_STATUS} NOT IN ({ph}) "
                    "AND cp.card_number NOT IN "
                    "    (SELECT card_number FROM valid_cards WHERE platform=?)",
                    list(CARD_STATUS_UNUSABLE) + [platform])
        return "", []

    def get_by_group(self, platform, group_id, page=1, page_size=20, bucket=''):
        offset = (page - 1) * page_size
        frag, fparams = self._bucket_where(bucket, platform)
        where = "WHERE cp.group_id=?" + (f" AND {frag}" if frag else "")
        params = [platform, group_id] + fparams
        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_pool cp {_JOIN_CPS} {where}", params
        )
        total = total_row['cnt'] if total_row else 0
        rows = self.db.fetchall(
            f"SELECT {_CP_COLS}, {_EFF_STATUS} AS status "
            f"FROM card_pool cp {_JOIN_CPS} {where} "
            f"ORDER BY cp.id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def count_buckets(self, platform, group_id):
        """返回分组内各桶数量 {total, invalid, valid, unverified}。统计前刷新过期状态。"""
        self.refresh_expired_status(group_id)
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        u = list(CARD_STATUS_UNUSABLE)
        row = self.db.fetchone(
            f"""SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN {_EFF_STATUS} IN ({ph}) THEN 1 ELSE 0 END) AS invalid,
                SUM(CASE WHEN {_EFF_STATUS} NOT IN ({ph})
                         AND cp.card_number IN
                             (SELECT card_number FROM valid_cards WHERE platform=?)
                    THEN 1 ELSE 0 END) AS valid,
                SUM(CASE WHEN {_EFF_STATUS} NOT IN ({ph})
                         AND cp.card_number NOT IN
                             (SELECT card_number FROM valid_cards WHERE platform=?)
                    THEN 1 ELSE 0 END) AS unverified
              FROM card_pool cp {_JOIN_CPS} WHERE cp.group_id=?""",
            # 参数按占位符在 SQL 文本里出现的先后排，不按逻辑顺序：这条查询的
            # SELECT 子句里就带了占位符，而它排在 FROM ... JOIN 之前，所以 JOIN 的
            # platform 落在倒数第二个、而不是像其它查询那样打头。
            (*u,                 # invalid 的 IN
             *u, platform,       # valid 的 NOT IN + 子查询 platform
             *u, platform,       # unverified 的 NOT IN + 子查询 platform
             platform,           # JOIN 的 platform
             group_id),
        )
        if not row:
            return {'total': 0, 'invalid': 0, 'valid': 0, 'unverified': 0}
        return {
            'total': row['total'] or 0,
            'invalid': row['invalid'] or 0,
            'valid': row['valid'] or 0,
            'unverified': row['unverified'] or 0,
        }

    def delete_invalid_by_group(self, platform, group_id):
        """删除分组内该平台视角下的无效卡（expired 或本平台 invalid）。删前刷新过期状态。

        注意这是**物理删除**：卡在别的平台可能仍可用，删掉就一起没了。UI 上这个动作
        由用户显式触发，语义是「这批卡我不要了」，因此按当前平台的视角选取是合理的；
        但调用方应当在提示文案里说清楚它跨平台生效。
        """
        self.refresh_expired_status(group_id)
        ph = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        cursor = self.db.execute(
            f"""DELETE FROM card_pool WHERE id IN (
                    SELECT cp.id FROM card_pool cp {_JOIN_CPS}
                    WHERE cp.group_id=? AND {_EFF_STATUS} IN ({ph})
                )""",
            (platform, group_id, *CARD_STATUS_UNUSABLE),
        )
        self._invalidate_usable_cache()
        return cursor.rowcount

    def move_non_invalid_to_group(self, platform, source_group_ids, target_group_id,
                                  bucket='non_invalid'):
        """把源分组里指定桶的卡**移动**到目标分组，按卡号去重。
        bucket='non_invalid'（默认）=有效+未验证；bucket='valid'=仅有效卡。
        返回 {moved, deduped}：moved=移入的去重卡数，deduped=删除的重复行数。
        卡原状态随卡带走（改 group_id；平台状态挂在卡号上，天然跟随）。同号只保留一行
        入目标组，其余同号命中行删除，避免 UNIQUE(card_number, group_id) 冲突。"""
        if not source_group_ids:
            return {'moved': 0, 'deduped': 0}
        if bucket not in ('non_invalid', 'valid'):
            raise ValueError(f"不支持的桶: {bucket}")
        for gid in source_group_ids:
            self.refresh_expired_status(gid)
        ph_status = ','.join('?' * len(CARD_STATUS_UNUSABLE))
        ph_groups = ','.join('?' * len(source_group_ids))
        if bucket == 'valid':
            # 非无效 且 在本平台验证成功过
            frag = (f"{_EFF_STATUS} NOT IN ({ph_status}) "
                    "AND cp.card_number IN "
                    "    (SELECT card_number FROM valid_cards WHERE platform=?)")
            extra = (platform,)
        else:
            frag = f"{_EFF_STATUS} NOT IN ({ph_status})"
            extra = ()
        rows = self.db.fetchall(
            f"""SELECT cp.id, cp.card_number FROM card_pool cp {_JOIN_CPS}
                WHERE cp.group_id IN ({ph_groups})
                  AND {frag}
                ORDER BY cp.id""",
            (platform, *source_group_ids, *CARD_STATUS_UNUSABLE, *extra),
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
        self._invalidate_usable_cache()
        return {'moved': moved, 'deduped': deduped}

    # 桶 → 可移动性：'non_invalid' 不是 _bucket_where 的桶，单独拼（= 有效 + 未验证）
    MOVABLE_BUCKETS = ('unverified', 'valid', 'non_invalid')

    def move_bucket_to_group(self, platform, source_group_id, target_group_id, bucket, limit):
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
            frag, fparams = f"{_EFF_STATUS} NOT IN ({ph})", list(CARD_STATUS_UNUSABLE)
        else:
            frag, fparams = self._bucket_where(bucket, platform)

        rows = self.db.fetchall(
            f"""SELECT cp.id, cp.card_number FROM card_pool cp {_JOIN_CPS}
                WHERE cp.group_id=? AND {frag}
                ORDER BY cp.id ASC LIMIT ?""",
            [platform, source_group_id] + fparams + [limit],
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
        self._invalidate_usable_cache()
        return {'moved': moved, 'skipped': skipped}

    # ---------- 取卡 ----------

    def get_all_by_group(self, group_id, platform=None):
        """获取分组内所有卡片（原始行）。

        platform 为 None 时 status 列是 card_pool 自己的值（只可能是 expired/''），
        传了平台则换成该平台视角下的有效状态。
        """
        if platform is None:
            rows = self.db.fetchall(
                "SELECT * FROM card_pool WHERE group_id=? ORDER BY id", (group_id,)
            )
        else:
            rows = self.db.fetchall(
                f"SELECT {_CP_COLS}, {_EFF_STATUS} AS status "
                f"FROM card_pool cp {_JOIN_CPS} "
                f"WHERE cp.group_id=? ORDER BY cp.id",
                (platform, group_id),
            )
        return [dict(r) for r in rows]

    def get_cards_as_list(self, group_id, platform=None):
        """获取分组内所有卡片，转换为原有 card dict 格式"""
        rows = self.get_all_by_group(group_id, platform)
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
                'status': (r.get('status') or ''),
            })
        return cards

    def get_usable_cards_as_list(self, platform, group_id):
        """获取分组内可用于该平台的卡片：先按当前日期刷新过期状态，再按该平台视角剔除。

        返回 (usable_cards, unusable_cards)，两者都是 get_cards_as_list 的 dict 格式。
        排除的是 NOT_SELECTABLE（无效 + 已绑定），而非仅 UNUSABLE：已绑定的卡遵循
        「一卡一账号」不能再选，但它不是无效卡，界面的无效桶仍只按 UNUSABLE 归类。

        「已绑定」「无效」都只在本平台成立——同一张卡在别的平台照样出现在可选集里，
        这正是多平台改造要的效果。
        """
        # 键带库路径，理由同 _expiry_refreshed_at：类级缓存跨 Database 实例共享。
        key = (getattr(self.db, 'db_path', ''), platform, group_id)
        with self._usable_cache_lock:
            hit = self._usable_cache.get(key)
            if hit is not None and (time.monotonic() - hit[0]) < self._USABLE_CACHE_TTL_SEC:
                # 返回浅拷贝：调用方对列表做 append/remove 不该污染缓存
                # （dict 本身共享——没有调用方会去改卡的字段）。
                return list(hit[1]), list(hit[2])

        self.refresh_expired_status(group_id)
        cards = self.get_cards_as_list(group_id, platform)
        usable = [c for c in cards if c['status'] not in CARD_STATUS_NOT_SELECTABLE]
        unusable = [c for c in cards if c['status'] in CARD_STATUS_NOT_SELECTABLE]

        with self._usable_cache_lock:
            self._usable_cache[key] = (time.monotonic(), usable, unusable)
        return list(usable), list(unusable)

    def refresh_expired_status(self, group_id=None, force=False):
        """按当前日期重新判定过期卡并标记为 expired（卡会随时间推移过期，故取卡前刷新）。

        平台无关，故不需要 platform 参数：有效期是卡自己的属性。

        拆表后这里也不再需要「不覆盖 paid/invalid/bound」的判断了——那三个状态已经
        搬到 card_platform_state，与本列不再争同一个格子，两边可以各自为真。一张被
        绑走后又到期的卡，现在既是 bound 又是 expired，信息不再互相顶掉。

        ## 两处性能约束（2026-08-13）

        这个方法在**每次取卡**时都会被调用，而取卡在 8 个 worker 的热路径上，
        全部压在 Database 那把全局锁上。原实现是「拉出分组全部行 → Python 逐行判断
        → 每张过期卡单独 UPDATE」，在 3.2 万张卡的分组上要 300ms 以上，直接把整个
        进程的 DB 访问堵住：worker 大半时间在等锁（8 个 worker 只跑得动 4 个浏览器），
        连每秒轮询的 /api/status 都被挤到超时。两处针对性改动：

        1. **只扫未标记过期的行**（`status != 'expired'`）。过期是单向的、标了就不会
           回头，已标记的行每次重扫纯属浪费。配合 idx_card_pool_group_status 索引，
           稳态下这个查询几乎不返回行。
        2. **按分组节流**：同一分组 _EXPIRY_REFRESH_TTL_SEC 内只真正扫一次。卡的过期
           状态按**天**变化，秒级重复扫描没有任何意义。force=True 可跳过节流，供
           界面上的手动刷新用——那种场景用户就是要看最新结果。

        批量 UPDATE 也合并成一条 `WHERE id IN (...)`，省掉 N 次单行写。
        """
        now = time.monotonic()
        # 键要带上库路径：类级状态在多个 Database 实例之间是共享的（测试各用各的
        # 临时库、group_id 却都是 1），不区分库就会互相读到对方的节流记录。
        key = (getattr(self.db, 'db_path', ''), group_id if group_id is not None else '__all__')
        if not force:
            last = self._expiry_refreshed_at.get(key)
            if last is not None and (now - last) < self._EXPIRY_REFRESH_TTL_SEC:
                return 0

        # 只看还没被标成 expired 的行：过期是单向的，标过就不必再看
        if group_id is None:
            rows = self.db.fetchall(
                "SELECT id, expiry_month, expiry_year FROM card_pool "
                "WHERE COALESCE(status,'') != ?", (CARD_STATUS_EXPIRED,))
        else:
            rows = self.db.fetchall(
                "SELECT id, expiry_month, expiry_year FROM card_pool "
                "WHERE group_id=? AND COALESCE(status,'') != ?",
                (group_id, CARD_STATUS_EXPIRED),
            )

        expired_ids = [r['id'] for r in rows
                       if is_card_expired(r['expiry_month'], r['expiry_year'])]
        # 分批是因为 SQLite 的变量数上限（SQLITE_MAX_VARIABLE_NUMBER，老版本低至 999）
        for i in range(0, len(expired_ids), 500):
            chunk = expired_ids[i:i + 500]
            marks = ','.join('?' * len(chunk))
            self.db.execute(
                f"UPDATE card_pool SET status=? WHERE id IN ({marks})",
                (CARD_STATUS_EXPIRED, *chunk))

        if expired_ids:
            self._invalidate_usable_cache()
        self._expiry_refreshed_at[key] = now
        return len(expired_ids)

    # ---------- 状态标记（全部按平台） ----------

    def _set_platform_status(self, platform, card_number, status):
        self.db.execute(
            "INSERT INTO card_platform_state (card_number, platform, status, updated_at) "
            "VALUES (?, ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(card_number, platform) DO UPDATE SET "
            "  status=excluded.status, updated_at=excluded.updated_at",
            (card_number, platform, status),
        )
        self._invalidate_usable_cache()

    def get_platform_status(self, platform, card_number):
        row = self.db.fetchone(
            "SELECT status FROM card_platform_state WHERE card_number=? AND platform=?",
            (card_number, platform),
        )
        return (row['status'] or '') if row else ''

    def mark_status_by_number(self, platform, card_number, status):
        """按卡号标记该卡在此平台的状态（如支付成功后标为 'paid'）。

        expired 是平台无关的，走 mark_expired_by_number 写 card_pool。
        """
        if status == CARD_STATUS_EXPIRED:
            self.mark_expired_by_number(card_number)
            return
        self._set_platform_status(platform, card_number, status)

    def mark_invalid_by_number(self, platform, card_number):
        """在该平台把卡标为无效（支付被拒等卡自身原因）。

        底层不变式：**在本平台**验证成功过的卡（valid_cards 里有本平台记录）永不被标
        invalid。它已被证明在这个平台可用，再次被拒只应进入临时冷却（见
        CardPaymentStateModel.set_cooldown），而非永久作废。此守卫是所有「标无效」入口
        的最终收口——即便上层调用方漏判，有效卡也不会被误标。

        `WHERE platform=?` 这个限定是多平台改造里最要紧的一处。守卫原先查的是整张
        valid_cards 表，于是一张在 opencode 成功过的卡到了新平台被拒也永远标不成
        invalid——坏卡会一轮一轮被反复选中、反复拒付，把额度和风控配额都耗光。
        """
        row = self.db.fetchone(
            "SELECT 1 AS hit FROM valid_cards WHERE card_number=? AND platform=? LIMIT 1",
            (card_number, platform),
        )
        if row:
            return
        self._set_platform_status(platform, card_number, CARD_STATUS_INVALID)

    def mark_expired_by_number(self, card_number):
        """标记为已过期（有效期已过）。平台无关，写 card_pool。"""
        self._invalidate_usable_cache()
        self.db.execute(
            "UPDATE card_pool SET status=? WHERE card_number=?",
            (CARD_STATUS_EXPIRED, card_number),
        )

    def mark_bound_by_number(self, platform, card_number):
        """在该平台标为已绑定到某账号（一卡一账号），此后不再参与该平台的选卡。

        以下状态一律保留，因为它们的信息量都比「被绑过」更大：
          - 本平台的 invalid：记录着卡不可用及其归因，本就不会被选中
          - 本平台的 paid：证明该卡真实付款成功过，是卡可用性的最强证据
          - card_pool 的 expired：卡已过期，同样不会被选中

        被保留的 paid 卡不会因此被重复绑定——建任务时
        card_bindings.get_successfully_bound_card_numbers() 那层派生过滤仍然生效。
        """
        current = self.get_platform_status(platform, card_number)
        if current in (CARD_STATUS_INVALID, CARD_STATUS_PAID):
            return
        row = self.db.fetchone(
            "SELECT 1 AS hit FROM card_pool WHERE card_number=? AND COALESCE(status,'')=? LIMIT 1",
            (card_number, CARD_STATUS_EXPIRED),
        )
        if row:
            return
        self._set_platform_status(platform, card_number, CARD_STATUS_BOUND)

    # ---------- 杂项 ----------

    def delete_card(self, card_id):
        self._invalidate_usable_cache()
        self.db.execute("DELETE FROM card_pool WHERE id=?", (card_id,))

    def delete_by_group(self, group_id):
        self._invalidate_usable_cache()
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

    def get_locations_by_number(self, platform, card_number):
        """返回该卡号当前在卡池中的位置列表：[{group_id, group_name, status}]，
        status 为该平台视角下的有效状态。
        通常 0 或 1 条（add_cards 阻止跨组同号）；用于有效卡弹窗展示"池内分组/状态"。"""
        if not card_number:
            return []
        rows = self.db.fetchall(
            f"SELECT cp.group_id, g.name AS group_name, {_EFF_STATUS} AS status "
            f"FROM card_pool cp {_JOIN_CPS} "
            f"LEFT JOIN card_groups g ON g.id=cp.group_id "
            f"WHERE cp.card_number=?",
            (platform, card_number),
        )
        return [dict(r) for r in rows]
