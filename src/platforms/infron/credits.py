"""infron.ai 的余额与充值。

充值形态见 `.trellis/tasks/08-04-infron-adapter/research/infron-payment-form.md`：
Top Up 是同页两步弹窗，第二步嵌 **Stripe Payment Element**（不是 opencode 那种
整页跳转的 hosted Checkout），所以表单定位那部分不能照抄 opencode。
"""

import re
import time

from src.browser.monitor import step as _step

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
