"""结账页「选币种 / 选卡支付方式」对 Stripe 改版的抗性。

回归 2026-08-13：云机上连续 11 张卡全部报「选中 Card 支付方式失败」，页面其实完全
正常。抓到实机 DOM 后确认是 Stripe 改了结账页结构，撞出两个独立的坑：

  1. 币种控件换成一排 button.CurrencyOptionButton，文本形如 "HK$173.26" / "$21.23"。
     原正则 `\\$\\s?\\d` 对港币也命中（"HK$1" 里就有 `$1`），`.first` 取到港币按钮——
     而当前选中的那个带 disabled，点它稳定白等 5s timeout。
  2. accordion id 从 payment-method-accordion-item-title-card 换成 card-accordion-item
     / payment-method-label-card，旧选择器全部 count()==0 → 返回 False → 上层判
     outcome=error 跳过这张卡。而实机 DOM 里 Card 本就带 --selected、卡字段已渲染，
     根本没有任何东西需要点。

所以这两个函数的判据必须是「结果对不对」（币种是不是美元、卡字段在不在），
而不是「某个写死的选择器点没点着」。
"""

import pytest

from src.payments import stripe_checkout as sc


class _Count:
    """只需要 count() 的极简 locator。"""

    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class _CurrencyBtn:
    """一个 button.CurrencyOptionButton。"""

    def __init__(self, alt, text, active=False, disabled=False):
        self.alt = alt
        self.text = text
        self.cls = "Button CurrencyOptionButton" + (" is-active" if active else "")
        self.disabled = disabled
        self.clicked = False

    def locator(self, sel):
        return _Count(1 if (sel == "img[alt='US']" and self.alt == "US") else 0)

    def inner_text(self, timeout=None):
        return self.text

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def is_disabled(self):
        return self.disabled

    def click(self, timeout=None):
        if self.disabled:
            raise TimeoutError("element is not enabled")
        self.clicked = True


class _BtnList:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]


class _Elem:
    """页面上一个普通元素（存在与否 + class + 是否被点过）。"""

    def __init__(self, frame, sel, cls=""):
        self.frame = frame
        self.sel = sel
        self.cls = cls
        self.count_n = 1

    def count(self):
        return self.count_n

    @property
    def first(self):
        return self

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def click(self, timeout=None, force=False):
        self.frame.clicks.append(self.sel)


class _Frame:
    def __init__(self, currency=(), elems=None):
        self.currency = list(currency)
        self.elems = dict(elems or {})   # selector -> class 字符串
        self.clicks = []

    def locator(self, sel):
        if sel == "button.CurrencyOptionButton":
            return _BtnList(self.currency)
        if sel in self.elems:
            return _Elem(self, sel, self.elems[sel])
        return _Count(0)

    def get_by_role(self, role, name=None):
        return _Count(0)

    def evaluate(self, script):
        return {"url": "https://checkout.stripe.com/c/pay/x", "ids": [], "btns": []}


class _Session:
    def capture_frame(self):
        pass


@pytest.fixture
def patched(monkeypatch):
    """把 _stripe_frame 指向测试构造的假 frame；就绪等待按「页面上有元素」判定。"""
    holder = {}

    def install(frame):
        holder["fr"] = frame
        monkeypatch.setattr(sc, "_stripe_frame", lambda *a, **k: frame)
        monkeypatch.setattr(
            sc, "wait_checkout_form_ready",
            lambda session, timeout=25, mark=sc.CHECKOUT_MARK: bool(frame.elems))
        return frame

    return install


# ---------------------------------------------------------------- 币种


def test_picks_usd_not_hkd(patched):
    """实机形状：港币已选中(is-active+disabled)、美元可点。必须点美元、绝不碰港币。"""
    hkd = _CurrencyBtn("HK", "HK$173.26", active=True, disabled=True)
    usd = _CurrencyBtn("US", "$21.23")
    patched(_Frame(currency=[hkd, usd], elems={"#cardNumber": ""}))

    assert sc.pick_currency_usd(_Session(), None) == "已选美金"
    assert usd.clicked
    assert not hkd.clicked


def test_usd_already_active_is_not_clicked(patched):
    """美元已是当前币种：直接返回，不去点那个 disabled 按钮（点了就是白等 5s timeout）。"""
    usd = _CurrencyBtn("US", "$21.23", active=True, disabled=True)
    hkd = _CurrencyBtn("HK", "HK$173.26")
    patched(_Frame(currency=[usd, hkd], elems={"#cardNumber": ""}))

    assert sc.pick_currency_usd(_Session(), None) == "已是美元"
    assert not usd.clicked
    assert not hkd.clicked


def test_usd_matched_by_amount_text_without_flag(patched):
    """没有国旗 img 时按金额文本判：只认行首 $，"CA$28.50" 这类不算美元。"""
    cad = _CurrencyBtn("", "CA$28.50")
    usd = _CurrencyBtn("", "$21.23")
    patched(_Frame(currency=[cad, usd], elems={"#cardNumber": ""}))

    assert sc.pick_currency_usd(_Session(), None) == "已选美金"
    assert usd.clicked
    assert not cad.clicked


# ---------------------------------------------------------------- 选卡


def test_card_already_selected_needs_no_click(patched):
    """卡字段已渲染 = Card 本就是当前支付方式：判成功且一次都不点。"""
    fr = patched(_Frame(elems={
        "#cardNumber": "",
        "[data-testid='card-accordion-item']":
            "AccordionItem " + sc._CARD_SELECTED_MARK,
    }))

    assert sc.select_card_method(_Session(), None) is True
    assert fr.clicks == []


def test_clicks_new_accordion_id_when_card_not_yet_rendered(patched):
    """卡字段还没出来时，点新版 accordion；点完字段出现即算成功。"""
    fr = _Frame(elems={"[data-testid='card-accordion-item']": "AccordionItem"})

    def click(timeout=None, force=False):
        fr.clicks.append("[data-testid='card-accordion-item']")
        fr.elems["#cardNumber"] = ""     # 点完卡字段才渲染出来

    orig = fr.locator

    def locator(sel):
        loc = orig(sel)
        if sel == "[data-testid='card-accordion-item']":
            loc.click = click
        return loc

    fr.locator = locator
    patched(fr)

    assert sc.select_card_method(_Session(), None) is True
    assert fr.clicks == ["[data-testid='card-accordion-item']"]


def test_old_accordion_id_still_supported(patched):
    """旧 id 仍在候选里——不同 Stripe 账户/版本可能还是旧结构。"""
    fr = _Frame(elems={"#payment-method-accordion-item-title-card": ""})

    orig = fr.locator

    def locator(sel):
        loc = orig(sel)
        if sel == "#payment-method-accordion-item-title-card":
            def click(timeout=None, force=False):
                fr.clicks.append(sel)
                fr.elems["#cardNumber"] = ""
            loc.click = click
        return loc

    fr.locator = locator
    patched(fr)

    assert sc.select_card_method(_Session(), None) is True


def test_blank_page_still_fails(patched):
    """页面真的空（表单没渲染出来）仍要判失败——否则会拿空表单去点 Pay。"""
    patched(_Frame())
    assert sc.select_card_method(_Session(), None) is False
