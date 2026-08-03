"""
通用工具函数模块
"""

import random
import string
import re
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import cfg


# 底料卡状态（card_pool.status）
CARD_STATUS_PAID = 'paid'         # 已成功支付过
CARD_STATUS_EXPIRED = 'expired'   # 有效期已过 → 无效
CARD_STATUS_INVALID = 'invalid'   # 支付被拒（卡本身问题）→ 无效
CARD_STATUS_FAILED = 'failed'     # 历史遗留：支付失败但未归因
CARD_STATUS_BOUND = 'bound'       # 已绑定到某个账号 → 一卡一账号，不再参与选卡

# 视为「无效」的状态。仅这两个算无效——卡池界面的无效桶按此归类，
# 已绑定的卡是正常消耗掉的，不该被展示成无效卡。
CARD_STATUS_UNUSABLE = (CARD_STATUS_EXPIRED, CARD_STATUS_INVALID)

# 选卡时要排除的全部状态：无效卡 + 已绑定卡。
# 「一卡一账号」此前只在建任务时由 card_bindings 实时派生（见
# get_successfully_bound_card_numbers），卡池自身无记录，于是界面看不出卡已被用掉，
# 且不走那条过滤的入口（如启动校验）会把已绑卡算进可用数。
CARD_STATUS_NOT_SELECTABLE = CARD_STATUS_UNUSABLE + (CARD_STATUS_BOUND,)


# ---- 账号终态：分身份层与平台层两组，别再各处硬编码 ----
# 拆成两组是多平台的必然结果：GitHub 被封是身份层的事，对所有平台一致；充值/订阅
# 完成是平台层的事，同一邮箱在 A 平台已充值不代表 B 平台也不用跑了。
#
# 此前这两层混在 accounts.status 一列里，各处自行硬编码过滤集合，且已经写岔了——
# app._payable_now 用四元组 ('banned','archived','flagged','recharged')，
# routes.start_daily_pipeline 的启动门只用了 ('banned','archived')，于是「启动时说有
# N 个可充值账号，实际跑起来只有 M 个」。统一到这里。

# 身份层终态：GitHub 侧已不可用，任何平台都别再拿它去跑
IDENTITY_TERMINAL_STATUSES = ('banned', 'suspended', 'rejected', 'flagged')

# 平台层终态：该账号在这个平台上本轮无需再处理
PLATFORM_TERMINAL_STATUSES = ('archived', 'recharged', 'subscribed')


def is_identity_terminal(identity_status):
    return (identity_status or '') in IDENTITY_TERMINAL_STATUSES


def is_platform_terminal(platform_status):
    return (platform_status or '') in PLATFORM_TERMINAL_STATUSES


# ---- 绑卡失败归因：只有卡自身的问题才该把底料卡标为 invalid ----
# 误标的代价是不可逆的：好卡被打成 invalid 后再也不会被选中。因此采用白名单，
# 只在确信是卡的问题时才归因，拿不准一律不标。

# 确定是卡的问题：银行拒付 / 卡号·CVC·有效期无效
_CARD_FAULT_PREFIXES = ('[外部原因]', '[表单字段错误]')

# 确定与卡无关：自动化流程、人机验证、浏览器、超时
_ENV_FAULT_PREFIXES = ('[操作失败]', '[验证超时]', '[浏览器中断]', '[超时]')

# 前缀不足以判定时（[Stripe字段错误] / [控制台表单错误] 既可能是拒卡，
# 也可能是像「缺手机号」这种与卡无关的表单校验），再按文案判定
_CARD_FAULT_PHRASES = (
    'declined', 'do not honor', 'insufficient funds', 'lost or stolen',
    'pickup card', 'call issuer', 'restricted card', 'card not supported',
    'security code is incorrect', 'incorrect cvc', 'cvc is invalid',
    'card number is invalid', 'invalid card number', 'is not a valid card number',
    'expiration date is in the past', 'expiration year is invalid',
    'expiration month is invalid', 'card has expired', 'expired card',
    '被拒', '资金不足', '余额不足', '安全码错误', '安全码不正确', '安全码无效',
    '卡号错误', '卡号无效', '卡号不正确', '有效期已过', '卡已过期',
)

# 出现这些字样时明确不是卡的问题——即便前缀看着像。
# 'mobile phone number' 是真实踩过的坑：Stripe Link 勾选框要求填手机号，
# 报错被打上 [Stripe字段错误] 前缀，按前缀归因会把一整批好卡全标成无效。
_NOT_CARD_PHRASES = (
    'mobile phone number', 'phone number', 'captcha', 'turnstile',
    'required field', '人机验证', '手机号',
)


def is_card_fault(err_reason):
    """判断一次绑卡/支付失败是否应归咎于卡本身（据此把底料卡标为 invalid）。

    拿不准时返回 False：漏标只是下次还会再试一次这张卡，误标则永久废掉一张好卡。
    """
    if not err_reason:
        return False
    text = str(err_reason)
    low = text.lower()

    for phrase in _NOT_CARD_PHRASES:
        if phrase in low or phrase in text:
            return False

    if text.startswith(_ENV_FAULT_PREFIXES):
        return False
    if text.startswith(_CARD_FAULT_PREFIXES):
        return True

    for phrase in _CARD_FAULT_PHRASES:
        if phrase in low or phrase in text:
            return True
    return False


def is_card_expired(expiry_month, expiry_year, today=None):
    """
    判断信用卡有效期是否已过期（当月最后一天前仍有效）。

    宽松解析：月份接受 "1"/"01"，年份接受 "26"/"2026"。
    解析不出合法年月时按无效处理（返回 True）——宁可跳过一张脏数据卡，
    也不要拿它去支付触发拒付、污染账号的失败次数。
    """
    try:
        month = int(str(expiry_month).strip())
        year_raw = str(expiry_year).strip()
        year = int(year_raw)
    except (TypeError, ValueError):
        return True

    if not 1 <= month <= 12:
        return True

    if year < 100:            # 两位年 "26" → 2026
        year += 2000
    if not 2000 <= year <= 2100:
        return True

    today = today or date.today()
    return (year, month) < (today.year, today.month)


def filter_expired_cards(cards, card_pool_model=None):
    """从卡列表中剔除不可用卡：有效期已过的，以及底料池里已标记为无效/过期的。

    传入 card_pool_model 时，会把新发现的过期卡在底料池中标记为 expired。
    返回 (usable, unusable)。
    """
    usable, unusable = [], []
    for card in cards or []:
        status = (card.get('status') or '')
        expired = is_card_expired(card.get('expiry_month'), card.get('expiry_year'))
        if not expired and status not in CARD_STATUS_UNUSABLE:
            usable.append(card)
            continue
        unusable.append(card)
        number = card.get('number') or card.get('card_number')
        if expired and status != CARD_STATUS_EXPIRED and card_pool_model and number:
            try:
                card_pool_model.mark_expired_by_number(number)
            except Exception:
                pass
    return usable, unusable


def create_http_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=cfg.retry.http_max_retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


http_session = create_http_session()


def get_user_agent():
    return cfg.browser.user_agent


def generate_random_password(length=None):
    if length is None:
        length = cfg.password.length

    chars = cfg.password.charset
    password = ''.join(random.choice(chars) for _ in range(length))
    password = (
        random.choice(string.ascii_uppercase) +
        random.choice(string.ascii_lowercase) +
        random.choice(string.digits) +
        random.choice("!@#$%") +
        password[4:]
    )
    print(f"Generated password: {password}")
    return password


def extract_verification_code(content: str):
    if not content:
        return None

    # 码长不固定：注册验证码为 6 位，登录二次验证（two-factor）为 7 位。
    # 兜底模式用 (?<!\d)...(?!\d) 锚定数字边界，否则 \d{6} 会把 7 位码
    # 5221150 截成 522115，得到一个必然失败的码。
    patterns = [
        r'login token[:\s]*(\d{6,8})',
        r'code is\s*(\d{6,8})',
        r'verification code[:\s]*(\d{6,8})',
        r'(?<!\d)(\d{6,8})(?!\d)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            code = matches[0]
            print(f"  Found verification code: {code}")
            return code

    return None
