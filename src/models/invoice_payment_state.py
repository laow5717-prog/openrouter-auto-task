"""
账单（invoice）支付状态模型

承载「账单已无法在 Stripe 支付」的冷却/永久标记：
- 支付页出现 "This invoice can no longer be paid on Stripe..." → 标 24h 冷却，
  冷却期内的后续充值直接跳过该发票、转去支付新账单，避免每次都白白重开支付页；
  到期自动恢复（now >= unpayable_until 即视为可再试），以防站点之后重新签发同号账单。
- 支付页被重定向到 Stripe Dashboard 登录页（订单已彻底无效）→ 标 10 年（hours 传大值），
  等同永久跳过、以后不再对该发票发起支付。
"""


class InvoicePaymentStateModel:
    def __init__(self, db):
        self.db = db

    def mark_unpayable(self, invoice_id, email='', hours=24, reason='', pay_url=''):
        """标记该账单进入「无法支付」冷却，到期时间 = now + hours。幂等（同号再标覆盖到期）。"""
        if not invoice_id:
            return
        self.db.execute(
            "INSERT INTO invoice_payment_state "
            "  (invoice_id, email, unpayable_until, reason, pay_url, updated_at) "
            "VALUES (?, ?, datetime('now','localtime',?), ?, ?, datetime('now','localtime')) "
            "ON CONFLICT(invoice_id) DO UPDATE SET "
            "  email=excluded.email, unpayable_until=excluded.unpayable_until, "
            "  reason=excluded.reason, pay_url=excluded.pay_url, updated_at=excluded.updated_at",
            (invoice_id, email or '', f'+{int(hours)} hours', (reason or '')[:200], (pay_url or '')[:500]),
        )

    def in_cooldown(self, invoice_id):
        """当前是否处于「无法支付」冷却中（now < unpayable_until）。到期即视为可再试。"""
        if not invoice_id:
            return False
        row = self.db.fetchone(
            "SELECT 1 AS x FROM invoice_payment_state WHERE invoice_id=? "
            "AND unpayable_until IS NOT NULL AND unpayable_until > datetime('now','localtime')",
            (invoice_id,),
        )
        return bool(row)

    def get_unpayable_until(self, invoice_id):
        if not invoice_id:
            return None
        row = self.db.fetchone(
            "SELECT unpayable_until FROM invoice_payment_state WHERE invoice_id=?", (invoice_id,)
        )
        return row['unpayable_until'] if row and row['unpayable_until'] else None
