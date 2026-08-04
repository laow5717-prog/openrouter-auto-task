"""平台适配器协议。

一个「平台」是我们要在上面开账号、充值/订阅的目标站点（opencode、infron.ai …）。
适配器负责该站点特有的那部分：怎么登录、租户 id 长什么样、余额在哪读、充值入口怎么
点、以及**怎么判断这笔款到底付成没有**。付款表单本身归支付供应商层
（src.payments.stripe_checkout），身份供给归 src.identity。

## 必需接口与可选能力

`PlatformAdapter` 只声明**每个平台都得有**的东西。可选能力单独拆协议
（目前是 `SubscribingAdapter`），由 `capabilities` 声明、编排层据此判断走不走。

这条是接第二个平台时才浮出来的：infron 纯充值制、没有订阅，若把 subscribe 声明成
必需方法，它就无法满足协议——而 capabilities 机制的本意恰恰是让这类能力可选。
将来再有「只有某些平台才有」的能力，照此办理，别往 PlatformAdapter 里塞。

## 接口为什么只有 7 个方法

调研时列过一份 12 个方法的候选清单，其中 auth_entry_urls / click_oauth_entry /
balance_url / start_payment / detect_payment_outcome / detect_subscription_outcome
全部只被 ensure_session / top_up / subscribe 这三个编排方法在内部调用，没有外部
调用者。把它们暴露成接口，等于强迫第二个平台按 opencode 的内部步骤分解自己的流程
——那正是抽象要避免的事。它们留作各适配器的私有实现细节。

唯一的例外是 read_balance_from_current_page：它确实被 API 层跨模块直接调用
（手动开浏览器时轮询余额落库），所以必须进接口。

## outcome 的语义不可改动

PaymentResult.outcome 的六个取值直接决定编排层「这张卡消耗不消耗」，每一条都是
线上事故换来的：

    success      付款成功
    failed       明确拒付 → 卡在本平台无条件进冷却，连续失败达阈值才判废
                 （阈值/冷却时长见 config.RechargeConfig；成功一次即清零）
    needs_captcha 账号级风控拦截 → 立即停手，**不消耗卡**
    error        付款**前**的页面故障 → **不消耗卡**
    unknown      未定案 → **不消耗卡**
    dry_ready    演练模式：填完卡未提交

后三者「不消耗卡」是硬约束。新平台的适配器必须按同样的语义归类自己的失败，
否则一次网络抖动就会把好卡判成废卡。
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

# 能力标记。编排层据此跳过平台不支持的流程，而不是靠捕获 AttributeError。
CAP_TOPUP = 'topup'          # 支持按金额充值
CAP_SUBSCRIBE = 'subscribe'  # 支持订阅套餐

# outcome 取值
OUTCOME_SUCCESS = 'success'
OUTCOME_FAILED = 'failed'
OUTCOME_NEEDS_CAPTCHA = 'needs_captcha'
OUTCOME_ERROR = 'error'
OUTCOME_UNKNOWN = 'unknown'
OUTCOME_DRY_READY = 'dry_ready'

# 不消耗卡的 outcome：不是这张卡的问题，别动它的状态。
OUTCOMES_KEEPING_CARD = (OUTCOME_NEEDS_CAPTCHA, OUTCOME_ERROR, OUTCOME_UNKNOWN)


@dataclass
class SessionResult:
    """建立平台会话的结果。

    blocked_by_identity 表示卡在**身份供给**那一层而不是平台这一层——opencode 的
    "account is flagged" 就是 GitHub 侧的反滥用标记，换哪个平台都授权不了。编排层
    据此写 accounts.identity_status 而不是平台状态。
    """
    ok: bool
    tenant_id: Optional[str] = None
    blocked_by_identity: bool = False
    detail: str = ''


@dataclass
class PaymentResult:
    ok: bool
    outcome: str
    err: str = ''
    last4: str = ''
    mode: Optional[str] = None
    balance_after: Optional[float] = None
    # **实际扣款金额**，可能不等于编排层传进来的 amount。
    #
    # 存在理由是 opencode 的首充：billing 页的 "Enable Billing" 只是跳转到后端预先
    # 建好的 Stripe Checkout，金额由站点定死（$20），我们传的 amount 根本没有落点；
    # 只有复充（"Add Balance" → 金额输入框）才认。若编排层拿「想充多少」去记账，
    # 账面会写着 $79 而实际只扣了 $20——2026-08-04 线上就是这样对不上的。
    #
    # 由**适配器**回报而不是让编排层按 mode 去推，是因为「首充固定 20」是纯粹的站点
    # 知识；编排层一旦开始按 mode 分支，就等于把 opencode 的规则焊进了平台无关的骨架，
    # 下一个平台必然踩坑。None 表示适配器没说，编排层此时沿用请求金额。
    amount: Optional[float] = None
    steps: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        """把既有浏览器层返回的 dict 收敛成 dataclass。

        适配器内部仍可沿用原来的 dict 流程，只在出口处转一次——这样收编现有实现
        不需要改动那些踩坑换来的判定逻辑。
        """
        d = d or {}
        return cls(
            ok=bool(d.get('ok')),
            outcome=d.get('outcome') or OUTCOME_UNKNOWN,
            err=d.get('err') or '',
            last4=d.get('last4') or '',
            mode=d.get('mode'),
            balance_after=d.get('balance_after'),
            amount=d.get('amount'),
            steps=list(d.get('steps') or []),
        )

    @property
    def keeps_card(self):
        """本次结果是否**不该**消耗这张卡。"""
        return self.outcome in OUTCOMES_KEEPING_CARD


@dataclass
class Credentials:
    """建立会话所需的凭据。字段全部来自身份层（accounts 表）。"""
    email: str
    login_password: Optional[str] = None   # OAuth 平台是 GitHub 密码；独立注册的平台是自家密码
    email_password: Optional[str] = None
    verify_link: Optional[str] = None      # 若安收信链接，用于自动过新设备邮箱验证


@runtime_checkable
class PlatformAdapter(Protocol):
    slug: str
    display_name: str
    capabilities: frozenset

    # 平台参数。原先散落在 OPENCODE_* 环境变量里，各平台的风控阈值本就不同。
    max_card_attempts: int        # 单账号单次最多试几张卡（防 velocity 风控）
    recharge_skip_balance: float  # 登录后实时余额 ≥ 此值即跳过充值并归档
    default_topup_amount: float

    def module_names(self) -> list:
        """本适配器涉及的模块名，供日志劫持（AppState._patch_prints）使用。"""
        ...

    def extract_tenant_id(self, url: str): ...

    def ensure_session(self, session, creds: Credentials, monitor=None,
                       timeout: int = 240) -> SessionResult: ...

    def read_balance(self, session, tenant_id, monitor=None): ...

    def read_balance_from_current_page(self, session): ...

    def fetch_apikey(self, session, tenant_id, monitor=None):
        """抓取该租户的 API key 明文；抓不到返回 None。

        编排层在充值成功 / 余额达标归档后调用（此时会话必在登录态），把 key 落到
        platform_accounts.apikey，免得事后再为每个账号单独开浏览器补抓。
        """
        ...

    def top_up(self, session, tenant_id, card, amount=None, monitor=None,
               should_stop=None) -> PaymentResult: ...


@runtime_checkable
class SubscribingAdapter(Protocol):
    """订阅能力。**可选**——只有 capabilities 含 CAP_SUBSCRIBE 的平台才需要实现。

    单独拆出来而不是塞进 PlatformAdapter，是接第二个平台时发现的：infron 是纯充值制、
    没有订阅，把 subscribe 声明成必需方法会让它无法满足协议，而 capabilities 这套
    机制的本意恰恰就是让这类能力可选。两者不能同时成立。

    编排层已经在调用前检查 capabilities（见 AppState._subscribe_one_account），
    所以这里拆开不需要改编排层。
    """

    def subscribe(self, session, tenant_id, card, monitor=None, should_stop=None,
                  dry: bool = False) -> PaymentResult: ...
