"""
账单支付选卡状态模型

目前仅承载 R3「3DS 临时冷却」：曾支付成功的卡再遇 3DS 时，标一个到期时间（默认 24h），
到期自动恢复可用，而不是像普通卡自身问题那样永久作废。

一卡一账号绑定（R1）由 valid_cards.source_email 派生、单卡次数冷却（R2）由 recharge_logs
实时统计，均无需在此落库。
"""


class CardPaymentStateModel:
    def __init__(self, db):
        self.db = db

    def set_tds(self, card_number, hours=24, reason=''):
        """标记该卡进入 3DS 临时冷却，到期时间 = now + hours。幂等（同卡再标覆盖到期）。"""
        if not card_number:
            return
        self.db.execute(
            "INSERT INTO card_payment_state (card_number, tds_until, tds_reason, updated_at) "
            "VALUES (?, datetime('now','localtime',?), ?, datetime('now','localtime')) "
            "ON CONFLICT(card_number) DO UPDATE SET "
            "  tds_until=excluded.tds_until, tds_reason=excluded.tds_reason, updated_at=excluded.updated_at",
            (card_number, f'+{int(hours)} hours', (reason or '')[:200]),
        )

    def get_tds_until(self, card_number):
        if not card_number:
            return None
        row = self.db.fetchone(
            "SELECT tds_until FROM card_payment_state WHERE card_number=?", (card_number,)
        )
        return row['tds_until'] if row and row['tds_until'] else None

    def in_tds_cooldown(self, card_number):
        """当前是否处于 3DS 临时冷却中（now < tds_until）。到期即视为可用。"""
        if not card_number:
            return False
        row = self.db.fetchone(
            "SELECT 1 AS x FROM card_payment_state WHERE card_number=? "
            "AND tds_until IS NOT NULL AND tds_until > datetime('now','localtime')",
            (card_number,),
        )
        return bool(row)

    def get_state_map(self):
        """批量取所有卡的 3DS 状态，供前端展示/选卡使用：
        返回 {card_number: {'tds_until': str, 'tds_reason': str, 'in_cooldown': bool}}"""
        rows = self.db.fetchall(
            "SELECT card_number, tds_until, tds_reason, "
            "  (tds_until IS NOT NULL AND tds_until > datetime('now','localtime')) AS in_cooldown "
            "FROM card_payment_state"
        )
        return {r['card_number']: {
            'tds_until': r['tds_until'],
            'tds_reason': r['tds_reason'],
            'in_cooldown': bool(r['in_cooldown']),
        } for r in rows}
