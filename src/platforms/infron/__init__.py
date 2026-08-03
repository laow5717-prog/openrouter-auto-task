"""infron.ai 平台适配器。

与 opencode 同类的 AI 模型聚合网关，**纯 credits 充值制、无订阅**，
所以 capabilities 只有 CAP_TOPUP。

## 必须走 AdsPower 指纹环境

infron 的入口挂着 Cloudflare Turnstile。实测 **Patchright 持久 profile 与本地
Chrome 都过不去**，会永远停在 "Just a moment..." 质询页；AdsPower 指纹环境约 30 秒
自动放行。

拿 `create_driver` 调试 infron 会白白卡住，而现象看起来像网络慢——这是本适配器
最容易踩的坑，写在最显眼处。

## 与 opencode 的三处结构差异

1. **登录是邮箱 magic link**，首次登录自动建号。不需要密码、不需要 GitHub。
   所以 `Credentials.login_password` 在这里没用，`verify_link`（ruoanzhu 收信链接）
   才是必需的。
2. **没有租户 id**。控制台就是 `/dashboard`，URL 里没有类似 opencode `wrk_xxx` 的段，
   故 `extract_tenant_id` 恒返回 None，`SessionResult.tenant_id` 也是 None。
3. **付款是嵌入式 Stripe Payment Element**，不是整页跳转的 hosted Checkout，
   且默认选中的支付方式是 Alipay 而非 Card。

## 未实现

`fetch_apikey`：`/dashboard/apiKeys` 列表页只显示脱敏 key（`sk-BOK***w8F`），
拿不到明文。**故意不实现**而不是返回脱敏串——脱敏串落库会看起来像抓成功了，
实际不可用。编排层用 `getattr` + `try/except` 包着调用，不实现就自动跳过。

`subscribe`：infron 无订阅，capabilities 不含 CAP_SUBSCRIBE，编排层会跳过。
"""

import os

from src.platforms.base import CAP_TOPUP, SessionResult
from src.platforms.infron import credits as _credits
from src.platforms.infron import login as _login


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return float(default)


class InfronAdapter:
    slug = 'infron'
    display_name = 'infron.ai'
    capabilities = frozenset({CAP_TOPUP})

    # 比 opencode（8）保守：infron 的反欺诈行为未知，先小步试，跑通再调。
    max_card_attempts = _env_int('INFRON_RECHARGE_MAX_ATTEMPTS', 5)
    recharge_skip_balance = _env_float('INFRON_RECHARGE_SKIP_BALANCE', 20)
    # 充值弹窗的最低档位就是 $50。
    default_topup_amount = _env_float('INFRON_TOPUP_AMOUNT', 50)

    def module_names(self):
        """涉及的模块名，供日志劫持（AppState._patch_prints）使用。"""
        return [
            'src.platforms.infron.login',
            'src.platforms.infron.credits',
            'src.payments.stripe_checkout',
        ]

    # ---------- 会话 ----------

    def extract_tenant_id(self, url):
        """infron 没有租户/工作区 id 的概念，恒 None。见模块 docstring 第 2 条。"""
        return None

    def ensure_session(self, session, creds, monitor=None, timeout=240):
        ok, detail = _login.ensure_session(session, creds, monitor=monitor, timeout=timeout)
        if ok:
            return SessionResult(ok=True, tenant_id=None, detail=detail)
        # infron 没有「身份供给侧被封」这一类——它不依赖 GitHub，登录失败都是平台侧的事。
        return SessionResult(ok=False, tenant_id=None, blocked_by_identity=False, detail=detail)

    # ---------- 余额 ----------

    def read_balance(self, session, tenant_id, monitor=None):
        return _credits.read_balance(session, monitor)

    def read_balance_from_current_page(self, session):
        return _credits.read_balance_from_current_page(session)

    def fetch_apikey(self, session, tenant_id, monitor=None):
        """恒返回 None —— infron 的 key 页只显示脱敏串，拿不到明文。

        实现成「如实返回 None」而不是干脆不实现，是因为 None 在契约里已经有确切含义
        （「抓不到」），而 `/dashboard/apiKeys` 确实抓不到。返回脱敏串才是错的：
        那会落库成一个看起来抓到了、实际不可用的值。

        要拿明文得另想办法（点复制按钮读剪贴板 / 新建 key 时截取那一瞬），另开任务。
        """
        return None

    # ---------- 充值 ----------

    def top_up(self, session, tenant_id, card, amount=None, monitor=None, should_stop=None):
        """尚未实现（Stage 3）。返回 error 而不是抛异常。

        选 `error` 是有意的：按 outcome 契约，error 表示「付款**前**的故障，不是这张卡
        的问题」，编排层因此**不会消耗这张卡**——不判废、不冷却、不记账。若改成抛异常
        或返回 failed，一次误配置就会把好卡打成废卡，而那是不可逆的。
        """
        from src.platforms.base import OUTCOME_ERROR, PaymentResult
        return PaymentResult(
            ok=False,
            outcome=OUTCOME_ERROR,
            err='infron 充值尚未实现（见任务 08-04-infron-adapter 的 Stage 3）',
            last4=str(card.get('number', ''))[-4:] if card else '',
        )
