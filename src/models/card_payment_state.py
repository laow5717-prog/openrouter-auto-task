"""
账单支付选卡状态模型

承载「好卡的临时冷却」：一张曾支付成功过的卡（= 已证明可用的好卡），若因风控被临时拦下
——遇 3DS，或再次充值被拒（速率/velocity 限制）——就标一个到期时间（默认 24h），到期自动
恢复可用，而不是像从未成功过的坏卡那样永久作废。物理上用同一列 tds_until（本质是
「临时冷却到期时间」），3DS 与速率冷却共用；reason 区分来由。二者都只是「暂时别选、到期恢复」，
互不冲突（冷却中的卡不会被选中，也就不会再触发另一种冷却）。

**按平台隔离**，主键是 (card_number, platform)。3DS 是「商户 + 发卡行」共同决定的，
换平台就是换一个 Stripe 商户号，同一张卡不一定再触发；速率冷却同理，各平台的风控阈值
互不相干。若不隔离，opencode 上一次 3DS 会让这张卡在所有平台一起停摆 24 小时。

一卡一账号绑定由 valid_cards.source_email 派生、单卡成功历史由 recharge_logs 实时统计，
均无需在此落库。
"""


class CardPaymentStateModel:
    def __init__(self, db):
        self.db = db

    def set_cooldown(self, platform, card_number, hours=24, reason=''):
        """标记该卡在此平台进入临时冷却，到期时间 = now + hours。幂等（同卡再标覆盖到期）。

        用于好卡被风控临时拦下的两种情形：3DS、或「曾成功过、本次被拒」的速率冷却。"""
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
        """批量取该平台所有卡的冷却状态，供前端展示/选卡使用：
        返回 {card_number: {'tds_until': str, 'tds_reason': str, 'in_cooldown': bool}}"""
        rows = self.db.fetchall(
            "SELECT card_number, tds_until, tds_reason, "
            "  (tds_until IS NOT NULL AND tds_until > datetime('now','localtime')) AS in_cooldown "
            "FROM card_payment_state WHERE platform=?",
            (platform,),
        )
        return {r['card_number']: {
            'tds_until': r['tds_until'],
            'tds_reason': r['tds_reason'],
            'in_cooldown': bool(r['in_cooldown']),
        } for r in rows}
