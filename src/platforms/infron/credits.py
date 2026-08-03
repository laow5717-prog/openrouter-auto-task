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
