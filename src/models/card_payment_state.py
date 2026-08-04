"""
账单支付选卡状态模型

这张表承载**两件事**，都以 (card_number, platform) 为主键：

1. **临时冷却**（tds_until / tds_reason）——「暂时别选这张卡，到期恢复」。
   触发面有三种：遇 3DS、充值被拒、速率/velocity 限制。物理上共用同一列
   （本质是「冷却到期时间」），reason 区分来由。互不冲突，因为冷却中的卡不会
   被选中，也就不会再触发另一种冷却。

2. **连续失败计数**（fail_streak / last_fail_at）——「这张卡在这个平台连着栽了几次」。
   累计到阈值才把卡永久判废；中间任意一次成功即清零。此前的口径是「首次被拒即
   永久 invalid」，一次发卡行的瞬时抖动就能烧掉一张好卡。

两者配合出来的行为是：每次失败 → 冷却 24h + 计数 +1，连着 3 次才判废，
所以判废一张坏卡最快需要 3 天。这是有意的——宁可慢，不可误杀。

**按平台隔离**。3DS 是「商户 + 发卡行」共同决定的，换平台就是换一个 Stripe 商户号，
同一张卡不一定再触发；速率冷却与失败计数同理，各平台的风控阈值互不相干。若不隔离，
opencode 上一次 3DS 会让这张卡在所有平台一起停摆 24 小时。

一卡一账号绑定由 valid_cards.source_email 派生、单卡成功历史由 recharge_logs 实时统计，
均无需在此落库。
"""


class CardPaymentStateModel:
    def __init__(self, db):
        self.db = db

    def set_cooldown(self, platform, card_number, hours=24, reason=''):
        """标记该卡在此平台进入临时冷却，到期时间 = now + hours。幂等（同卡再标覆盖到期）。

        触发面有两类：遇 3DS，以及**任何一次充值被拒**（不再区分这张卡此前成功过没有
        ——「同一张卡两次使用至少隔 fail_cooldown_hours」是对所有卡一视同仁的规则，
        为的是不在发卡行那里连着撞 velocity 风控）。判废与否另由 fail_streak 决定。"""
        if not card_number:
            return
        self.db.execute(
            "INSERT INTO card_payment_state (card_number, platform, tds_until, tds_reason, updated_at) "
            "VALUES (?, ?, datetime('now','localtime',?), ?, datetime('now','localtime')) "
            "ON CONFLICT(card_number, platform) DO UPDATE SET "
            "  tds_until=excluded.tds_until, tds_reason=excluded.tds_reason, updated_at=excluded.updated_at",
            (card_number, platform, f'+{int(hours)} hours', (reason or '')[:200]),
        )

    def in_cooldown(self, platform, card_number):
        """该卡在此平台是否处于临时冷却中（now < tds_until）。到期即视为可用。"""
        if not card_number:
            return False
        row = self.db.fetchone(
            "SELECT 1 AS x FROM card_payment_state WHERE card_number=? AND platform=? "
            "AND tds_until IS NOT NULL AND tds_until > datetime('now','localtime')",
            (card_number, platform),
        )
        return bool(row)

    # 兼容旧命名（3DS 专名），语义与 set_cooldown/in_cooldown 相同。
    set_tds = set_cooldown
    in_tds_cooldown = in_cooldown

    def get_tds_until(self, platform, card_number):
        if not card_number:
            return None
        row = self.db.fetchone(
            "SELECT tds_until FROM card_payment_state WHERE card_number=? AND platform=?",
            (card_number, platform),
        )
        return row['tds_until'] if row and row['tds_until'] else None

    def get_state_map(self, platform):
        """批量取该平台所有卡的冷却状态与连续失败计数，供前端展示/选卡使用：
        返回 {card_number: {'tds_until', 'tds_reason', 'in_cooldown', 'fail_streak'}}"""
        rows = self.db.fetchall(
            "SELECT card_number, tds_until, tds_reason, COALESCE(fail_streak,0) AS fail_streak, "
            "  (tds_until IS NOT NULL AND tds_until > datetime('now','localtime')) AS in_cooldown "
            "FROM card_payment_state WHERE platform=?",
            (platform,),
        )
        return {r['card_number']: {
            'tds_until': r['tds_until'],
            'tds_reason': r['tds_reason'],
            'in_cooldown': bool(r['in_cooldown']),
            'fail_streak': r['fail_streak'] or 0,
        } for r in rows}

    # ---------- 连续失败计数 ----------

    def bump_fail_streak(self, platform, card_number):
        """该卡在此平台的连续失败次数 +1，返回**新值**。调用方据此决定是否判废。

        必须在**一个事务内**完成 upsert + 回读。db.execute 每次调用各自持锁并 commit，
        拆成两次调用的话，两个 worker 同时失败同一张卡时会各读到同一个旧值，计数只涨 1
        ——坏卡因此永远够不到判废阈值。

        ⚠️ 事务块内只能用 yield 出来的 conn，不能调 self.db.execute/fetchone：
        Database._lock 不可重入，会死锁（见 database.py 的 transaction 注释）。
        """
        if not card_number:
            return 0
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO card_payment_state "
                "  (card_number, platform, fail_streak, last_fail_at, updated_at) "
                "VALUES (?, ?, 1, datetime('now','localtime'), datetime('now','localtime')) "
                "ON CONFLICT(card_number, platform) DO UPDATE SET "
                "  fail_streak  = COALESCE(card_payment_state.fail_streak, 0) + 1, "
                "  last_fail_at = excluded.last_fail_at, "
                "  updated_at   = excluded.updated_at",
                (card_number, platform),
            )
            row = conn.execute(
                "SELECT COALESCE(fail_streak,0) AS n FROM card_payment_state "
                "WHERE card_number=? AND platform=?",
                (card_number, platform),
            ).fetchone()
        return (row['n'] if row else 0) or 0

    def reset_fail_streak(self, platform, card_number):
        """清零连续失败计数（充值成功时调）。

        只 UPDATE、不 INSERT：没有记录本就等于计数为 0，为一张一次没失败过的卡
        凭空插一行没有任何信息量，还会让 get_state_map 返回一堆全零的噪声行。
        冷却时间（tds_until）不动——那是另一件事，成功一次不该把别的原因造成的
        冷却一起抹掉。
        """
        if not card_number:
            return
        self.db.execute(
            "UPDATE card_payment_state SET fail_streak=0, "
            "  updated_at=datetime('now','localtime') "
            "WHERE card_number=? AND platform=?",
            (card_number, platform),
        )

    def get_fail_streak(self, platform, card_number):
        if not card_number:
            return 0
        row = self.db.fetchone(
            "SELECT COALESCE(fail_streak,0) AS n FROM card_payment_state "
            "WHERE card_number=? AND platform=?",
            (card_number, platform),
        )
        return (row['n'] if row else 0) or 0
