"""infron.ai 的余额与充值。

充值形态见 `.trellis/tasks/08-04-infron-adapter/research/infron-payment-form.md`：
Top Up 是同页两步弹窗，第二步嵌 **Stripe Payment Element**（不是 opencode 那种
整页跳转的 hosted Checkout），所以表单定位那部分不能照抄 opencode。
"""

import re
import time

from src.browser.monitor import step as _step
from src.payments.stripe_checkout import (
    ELEMENT_MARK,
    _captcha_challenge_present,
    _close_challenge_lightbox,
    _close_threeds_modal,
    _threeds_challenge_lightbox,
    _threeds_challenge_present,
    _threeds_failure_modal,
    _DECLINE_HINTS,
    fill_payment_element_card,
    select_card_tab,
)

CREDITS_URL = 'https://infron.ai/dashboard/credits'

# 余额区块。页面上是独立的 "Available Balance" 标题 + 下一行金额（小数点后 8 位），
# 比 opencode 那种 "$12.34 Current Balance" 的行内格式好抠。
_BAL_RE = re.compile(r'Available\s+Balance\s*\$?\s*([0-9][0-9,]*\.?[0-9]*)', re.I)

_TEXT_JS = "() => (document.body ? document.body.innerText : '') || ''"


def _page_text(session):
    try:
        return session.page.evaluate(_TEXT_JS) or ''
    except Exception:
        return ''


def read_balance_from_current_page(session):
    """从当前页面抠余额，不做任何导航。读不到返回 None。

    **余额为 0 要返回 0.0，不能返回 None。** 两者语义不同：0.0 是「读到了，账上没钱」，
    None 是「没读到」。编排层的归档预检拿到 None 会跳过判断继续充值，拿到 0.0 才会
    正确判定「未达阈值，该充」。把 0 当 None 会让逻辑在余额恰好为零时微妙地走错。
    """
    hit = _BAL_RE.search(_page_text(session))
    if not hit:
        return None
    try:
        return float(hit.group(1).replace(',', ''))
    except ValueError:
        return None


def read_balance(session, monitor=None, timeout=45):
    """导航到 credits 页并读余额。读不到返回 None。

    页面是 SPA，余额区块要等渲染，所以轮询而不是固定 sleep。
    """
    session.get(CREDITS_URL)
    _step(monitor, session, '打开 credits 页读余额')
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        value = read_balance_from_current_page(session)
        if value is not None:
            return value
    return None


# ---------------------------------------------------------------------------
# Top Up 弹窗
#
# 同一个模态框分两步（有 Back 可退回）：
#   第一步 选金额与支付方式 → 点 Pay
#   第二步 嵌 Stripe Payment Element 填卡 → 再点一次 Pay
# **两步的按钮同名**（都是 `Pay $X`），只能靠弹窗文案区分当前处在哪一步。
# ---------------------------------------------------------------------------

# 弹窗标题，两步都在
_MODAL_TITLE = 'Top Up Credits'
# 第一步的副标题
_STEP1_HINT = 'confirm details on the next step'
# 第二步的副标题
_STEP2_HINT = 'enter your card or another stripe payment method'

# 站点提供的档位。非档位金额走自定义输入框。
_PRESET_AMOUNTS = (50, 100, 300)

_MODAL_JS = """
() => {
  const ds = [...document.querySelectorAll('[role=dialog]')]
    .filter(d => (d.innerText || '').includes('Top Up Credits'));
  if (!ds.length) return null;
  return { text: ds[ds.length - 1].innerText || '' };
}
"""


def _modal_text(session):
    try:
        got = session.page.evaluate(_MODAL_JS)
    except Exception:
        return None
    return got['text'] if got else None


def current_step(session):
    """当前弹窗处在第几步：1 / 2 / None（弹窗未开）。"""
    text = (_modal_text(session) or '').lower()
    if not text:
        return None
    if _STEP2_HINT in text:
        return 2
    if _STEP1_HINT in text:
        return 1
    return 1        # 有弹窗但文案对不上，按第一步处理


def open_topup_modal(session, monitor=None, timeout=45):
    """进 credits 页并打开 Top Up 弹窗。成功返回 True。

    **弹窗要等够。** 实测点下去 10 秒内 DOM 只多一个 hCaptcha iframe，看起来像
    「点了没反应」——第一次探测就因此误判成「必须先绑卡」。15 秒以上才出来，
    所以这里轮询而不是固定 sleep，默认给到 45 秒。
    """
    session.get(CREDITS_URL)
    time.sleep(6)
    _step(monitor, session, '打开充值弹窗')
    try:
        session.page.get_by_role('button', name='Top Up').first.click(timeout=15000)
    except Exception as e:
        _step(monitor, session, f'点 Top Up 失败：{str(e)[:90]}')
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if _modal_text(session):
            _step(monitor, session, '充值弹窗已打开')
            return True
    _step(monitor, session, f'{timeout}s 内充值弹窗未出现')
    return False


def select_amount(session, amount, monitor=None):
    """选充值金额。命中档位就点按钮，否则填自定义输入框。返回是否成功。"""
    value = int(amount) if float(amount).is_integer() else float(amount)
    if value in _PRESET_AMOUNTS:
        try:
            session.page.get_by_role('button', name=f'${value}', exact=True).first.click(timeout=8000)
            _step(monitor, session, f'选中 ${value} 档')
            time.sleep(2)
            return True
        except Exception:
            pass        # 档位点不中就退到自定义输入
    try:
        # 自定义金额输入框是弹窗里唯一的可见 text/number input
        box = session.page.locator("[role=dialog] input:visible").first
        box.fill(str(value), timeout=8000)
        _step(monitor, session, f'填入自定义金额 ${value}')
        time.sleep(2)
        return True
    except Exception as e:
        _step(monitor, session, f'选金额失败：{str(e)[:90]}')
        return False


def select_card_payment(session, monitor=None):
    """在第一步选「Card」支付方式。点不中不算失败——它通常已是默认。"""
    try:
        session.page.get_by_role('button', name='Card', exact=True).first.click(timeout=6000)
        _step(monitor, session, '选中 Card 支付方式')
        time.sleep(2)
        return True
    except Exception:
        return False


def click_pay(session, monitor=None, timeout=15000):
    """点弹窗里的 Pay 按钮。

    **按 `Pay ` 前缀匹配，绝不认金额**：按钮文案带的是含手续费的总额
    （充 $100 显示 `Pay $105.35`），改充值额或站点调手续费都会让写死的选择器失效。
    """
    try:
        session.page.get_by_role('button', name=re.compile(r'^Pay\s')).first.click(timeout=timeout)
        _step(monitor, session, '已点击 Pay')
        return True
    except Exception as e:
        _step(monitor, session, f'点 Pay 失败：{str(e)[:90]}')
        return False


def wait_for_payment_step(session, monitor=None, timeout=60):
    """等弹窗进到第二步（填卡）。成功返回 True。

    进不去最常见的原因是 **hCaptcha 没解掉**——未装求解 hook 时 hCaptcha 会显示
    `Please try again` 并把 Stripe Payment Element 卡在加载之前，弹窗停在第一步。
    现象看起来像「页面加载慢」，实际是验证码。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if current_step(session) == 2:
            _step(monitor, session, '已进入填卡步骤')
            return True
    _step(monitor, session,
          f'{timeout}s 内未进入填卡步骤（多半是 hCaptcha 未解，Payment Element 加载不出来）')
    return False


# ---------------------------------------------------------------------------
# 结果判定
# ---------------------------------------------------------------------------

def detect_payment_result(session, balance_before, monitor=None, timeout=180, poll=6):
    """判定这笔充值成没成。返回 (outcome, detail, balance_after)。

    **成功判据是余额增长，不是页面文案。** 页面可能显示"处理中"就再无更新，也可能
    在真的扣款成功后仍停在弹窗上；余额是唯一不会骗人的信号。这条是从 opencode 那边
    照搬的，那套判定是一次次事故换来的。

    outcome 的语义与 opencode 逐字一致（见 platforms.base）：
      success        余额增加
      failed         明确拒付 / 3DS 认证失败
      needs_captcha  hCaptcha 挑战出现且没被解掉 —— 账号级拦截，**不消耗卡**
      unknown        已提交但超时无定论 —— **不消耗卡**
    """
    baseline = balance_before if balance_before is not None else 0.0
    deadline = time.time() + timeout
    saw_captcha = False

    while time.time() < deadline:
        time.sleep(poll)

        # 1) 余额涨了就是成功——最可靠的信号，优先判
        after = read_balance_from_current_page(session)
        if after is None:
            # 弹窗盖着 credits 页时读不到，去页面文本里再找一次
            after = read_balance_from_current_page(session)
        if after is not None and after > baseline + 1e-6:
            _step(monitor, session, f'余额已增加：{baseline} → {after}')
            return 'success', f'余额 {baseline} → {after}', after

        text = (_page_text(session) or '').lower()

        # 2) 明确拒付
        hit = next((h for h in _DECLINE_HINTS if h in text), None)
        if hit:
            _step(monitor, session, f'检测到拒付：{hit}')
            return 'failed', f'拒付：{hit}', None

        # 3) 3DS：失败弹窗算拒付；交互挑战则关掉后继续等
        try:
            fr = _threeds_failure_modal(session)
            if fr is not None:
                _close_threeds_modal(fr, session, monitor)
                return 'failed', '3DS 认证失败', None
            fr = _threeds_challenge_lightbox(session)
            if fr is not None:
                _close_challenge_lightbox(fr, session, monitor)
        except Exception:
            pass

        # 4) hCaptcha 挑战：记下但不立刻收手——求解器可能正在解
        try:
            if _threeds_challenge_present(session) is None and _captcha_challenge_present(session):
                saw_captcha = True
        except Exception:
            pass

    if saw_captcha:
        _step(monitor, session, '超时且期间出现过 hCaptcha 挑战')
        return 'needs_captcha', f'{timeout}s 内未确认结果，期间出现 hCaptcha 挑战', None
    return 'unknown', f'{timeout}s 内余额未增加，未确认成功', None


def top_up(session, card, amount, monitor=None, should_stop=None):
    """完整充值流程。返回 dict，字段对齐 PaymentResult.from_dict。

    每一步失败都归到 **error**（不消耗卡）而不是 failed——这些都是「还没走到付款」的
    页面故障，把它们算成拒付会白白废掉好卡，而判废不可逆。只有真的提交了付款、
    拿到明确拒付信号才是 failed。

    **本函数不让异常逃出去**（InterruptedError 除外，那是用户主动停止）。契约要求
    返回 PaymentResult；异常逃出去意味着编排层的 outcome 分派根本不会执行，那张卡的
    状态就悬着了。浏览器操作抛异常是常态（导航超时、frame 被卸载、元素消失），
    统一收敛成 error。
    """
    try:
        return _top_up_inner(session, card, amount, monitor, should_stop)
    except InterruptedError:
        raise
    except Exception as e:
        return {'ok': False, 'outcome': 'error',
                'err': f'充值过程异常：{type(e).__name__}: {str(e)[:150]}',
                'last4': str(card.get('number', ''))[-4:], 'steps': []}


def _top_up_inner(session, card, amount, monitor=None, should_stop=None):
    steps = []
    last4 = str(card.get('number', ''))[-4:]

    def _fail(detail):
        return {'ok': False, 'outcome': 'error', 'err': detail,
                'last4': last4, 'steps': steps}

    balance_before = read_balance(session, monitor)
    steps.append(f'balance_before={balance_before}')
    if should_stop and should_stop():
        raise InterruptedError('用户请求停止')

    if not open_topup_modal(session, monitor):
        return _fail('充值弹窗未打开')
    steps.append('modal_opened')

    if not select_amount(session, amount, monitor):
        return _fail('选充值金额失败')
    select_card_payment(session, monitor)
    steps.append(f'amount={amount}')

    if not click_pay(session, monitor):
        return _fail('第一步 Pay 点击失败')

    if not wait_for_payment_step(session, monitor):
        return _fail('未进入填卡步骤（多半是 hCaptcha 未解，Payment Element 加载不出来）')
    steps.append('payment_step')

    select_card_tab(session, monitor, mark=ELEMENT_MARK)
    ok, detail = fill_payment_element_card(session, card, monitor, mark=ELEMENT_MARK)
    steps.append(f'fill_card ok={ok}')
    if not ok:
        return _fail(f'填卡失败：{detail}')

    if should_stop and should_stop():
        raise InterruptedError('用户请求停止')
    if not click_pay(session, monitor):
        return _fail('第二步 Pay 点击失败')
    steps.append('submitted')

    outcome, detail, balance_after = detect_payment_result(session, balance_before, monitor)
    steps.append(f'outcome={outcome}')
    return {'ok': outcome == 'success', 'outcome': outcome, 'err': detail,
            'last4': last4, 'balance_after': balance_after, 'steps': steps}
