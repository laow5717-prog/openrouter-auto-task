"""opencode.ai 平台适配器。

把 login / billing / subscribe 三个模块包装成 PlatformAdapter。**只做转接，不改判定
逻辑**——那些逻辑是踩坑换来的（余额增长才算充值成功、订阅另有一套 URL 回落判据、
hCaptcha 与 3DS 的识别时序），照原样调用。

opencode 走 GitHub OAuth，没有自己的登录密码，所以 Credentials.login_password 在这里
是**GitHub 密码**；platform_accounts.login_password 对本平台恒为空。
"""

import os

from src.platforms.base import (
    CAP_SUBSCRIBE,
    CAP_TOPUP,
    PaymentResult,
    SessionResult,
)
from src.platforms.opencode import billing as _billing
from src.platforms.opencode import login as _login
from src.platforms.opencode import subscribe as _subscribe


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return float(default)


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


class OpencodeAdapter:
    slug = 'opencode'
    display_name = 'opencode.ai'
    capabilities = frozenset({CAP_TOPUP, CAP_SUBSCRIBE})

    # 单账号单次最多试几张卡。卡池上千张，不设限的话一批坏卡会在同一 workspace 上
    # 连续制造大量拒付，极易触发 Stripe/opencode 的反欺诈 velocity 风控。
    max_card_attempts = _env_int('OPENCODE_RECHARGE_MAX_ATTEMPTS', 8)
    # 登录后实时余额 ≥ 此值即跳过充值并归档（以实时余额为准，DB 值会过时）。
    recharge_skip_balance = _env_float('OPENCODE_RECHARGE_SKIP_BALANCE', 20)
    default_topup_amount = 20.0
    # 订阅链路的单账号卡数上限，比充值更保守——订阅拒付对账号的伤害更大。
    max_subscribe_attempts = 5

    def module_names(self):
        """涉及的模块名，供日志劫持（AppState._patch_prints）使用。"""
        return [
            'src.platforms.opencode.login',
            'src.platforms.opencode.billing',
            'src.platforms.opencode.subscribe',
            'src.payments.stripe_checkout',
        ]

    # ---------- 会话 ----------

    def extract_tenant_id(self, url):
        """从 URL 抠出 workspace id（wrk_xxx）；不在已登录区则 None。"""
        return _login._extract_wid(url)

    def ensure_session(self, session, creds, monitor=None, timeout=240):
        """确保处于登录态，返回 SessionResult。

        内部是完整的降级链：已登录复用 → GitHub 账号密码登录 → 新设备邮箱验证自动
        收码 → OAuth 授权。ensure_opencode_session 返回 (wid, detail)，detail 里带
        'flagged' 表示 GitHub 侧被反滥用标记——那是身份层的封禁，不是本平台的状态，
        所以映射到 blocked_by_identity 而不是普通失败。
        """
        wid, detail = _billing.ensure_opencode_session(
            session, monitor, creds.login_password, creds.email,
            verify_link=creds.verify_link,
        )
        if wid:
            return SessionResult(ok=True, tenant_id=wid, detail=detail or '')
        return SessionResult(
            ok=False,
            tenant_id=None,
            blocked_by_identity='flagged' in (detail or ''),
            detail=detail or '',
        )

    # ---------- 余额 ----------

    def read_balance(self, session, tenant_id, monitor=None):
        return _billing.read_current_balance(session, tenant_id, monitor)

    def read_balance_from_current_page(self, session):
        """从当前页面直接抠余额，不做任何导航。

        供「手动开浏览器」时的保活轮询用——用户自己点到结算页时顺手落库刷新余额。
        """
        return _billing._read_balance(session)

    # ---------- 充值 ----------

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        result = _billing.recharge_via_stripe(
            session, card, tenant_id,
            amount=self.default_topup_amount if amount is None else amount,
            monitor=monitor, should_stop=should_stop,
        )
        return PaymentResult.from_dict(result)

    # ---------- 订阅 ----------

    def subscribe(self, session, tenant_id, card, monitor=None, should_stop=None, dry=False):
        result = _subscribe.subscribe_via_stripe(
            session, card, tenant_id, monitor=monitor, should_stop=should_stop, dry=dry,
        )
        return PaymentResult.from_dict(result)
