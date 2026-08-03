"""零成本诊断：跑到 Stripe 结账页填完卡、**不点 Subscribe**（不触发 hCaptcha、不扣款），
检查承载 window.hcaptcha 的 b.stripecdn HCaptchaInvisible 帧里，hcaptcha 对象的方法能否被
后置改写——决定「逐帧 evaluate 后置 hook」这条路到底可行不可行。

判据：
- 若 frame[7] 此刻不存在 window.hcaptcha → hCaptcha 提交时才 render，后置 hook 无从谈起。
- 若存在但 execute/render/getResponse 的 descriptor writable=false 或对象 frozen，或试写后
  hc.execute !== 我们的函数 → 证实后置改写无效，必须前置注入（Patchright 已禁）。

用法: python3 scripts/probe_hcaptcha_obj.py --email carold030@hotmail.com --group 1
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.browser.driver import create_driver, close_driver
from src.platforms.opencode.login import login_and_open_own_go
from src.platforms.opencode.subscribe import start_subscribe_go, select_usd_subscribe
from src.platforms.opencode.billing import (
    select_card_method, fill_card_and_address, fill_phone_if_present,
    uncheck_save_info, check_ai_agent_consent)
from src.models.database import Database
from src.models.card_pool import CardPoolModel

# 探测 hcaptcha 对象可变性
_OBJ_PROBE_JS = r"""
() => {
  var r = {hasHcaptcha: false};
  try {
    var hc = window.hcaptcha;
    r.hasHcaptcha = typeof hc !== 'undefined' && hc !== null;
    if (!r.hasHcaptcha) return r;
    r.keys = Object.keys(hc).slice(0, 20);
    r.frozen = Object.isFrozen(hc);
    r.sealed = Object.isSealed(hc);
    r.extensible = Object.isExtensible(hc);
    // window.hcaptcha 属性本身的 descriptor（能否被我们 defineProperty 前置替换）
    var wd = Object.getOwnPropertyDescriptor(window, 'hcaptcha');
    r.winDesc = wd ? {configurable: wd.configurable, writable: wd.writable,
                      hasGet: !!wd.get, hasSet: !!wd.set} : null;
    // 各方法的 descriptor + 是否在自身还是原型
    r.methods = {};
    ['execute', 'render', 'getResponse', 'reset', 'getRespKey'].forEach(function(m){
      var own = Object.getOwnPropertyDescriptor(hc, m);
      var proto = null, p = Object.getPrototypeOf(hc);
      while (p && !proto) { proto = Object.getOwnPropertyDescriptor(p, m); p = Object.getPrototypeOf(p); }
      var d = own || proto;
      r.methods[m] = {exists: typeof hc[m] === 'function', onSelf: !!own,
                      writable: d ? d.writable : null, configurable: d ? d.configurable : null};
    });
    // 实测：能否把 execute 换成我们的函数并「粘住」
    try {
      var marker = function(){ return 'HOOKED'; };
      hc.execute = marker;
      r.assignStuck = (hc.execute === marker);
    } catch(e) { r.assignErr = String(e).slice(0,80); r.assignStuck = false; }
    try {
      var marker2 = function(){ return 'HOOKED2'; };
      Object.defineProperty(hc, 'execute', {configurable: true, writable: true, value: marker2});
      r.defineStuck = (hc.execute === marker2);
    } catch(e) { r.defineErr = String(e).slice(0,80); r.defineStuck = false; }
  } catch(e) { r.err = String(e).slice(0,120); }
  return r;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="carold030@hotmail.com")
    ap.add_argument("--group", type=int, default=1)
    args = ap.parse_args()

    usable, _ = CardPoolModel(Database()).get_usable_cards_as_list(args.group)
    if not usable:
        print("无可用卡"); sys.exit(1)
    card = usable[0]

    session = create_driver(headless=False, profile_id=args.email)
    try:
        lg = login_and_open_own_go(session)
        print("登录:", lg.get("ok"), lg.get("wid"))
        if not lg.get("ok"):
            sys.exit(1)
        ok, d = start_subscribe_go(session, lg["wid"])
        print("start_subscribe_go:", ok, d)
        print("currency:", select_usd_subscribe(session))
        print("select_card:", select_card_method(session, None))
        print("fill:", fill_card_and_address(session, card, None))
        fill_phone_if_present(session, card, None)
        uncheck_save_info(session, None)
        check_ai_agent_consent(session, None)
        print("== 已填完卡，不点 Subscribe。等 3s 后逐帧探测 hcaptcha 对象可变性 ==")
        time.sleep(3)

        found = False
        for i, fr in enumerate(session.page.frames):
            url = (fr.url or "")
            if "hcaptcha" not in url.lower() and "HCaptchaInvisible" not in url and "stripecdn" not in url:
                continue
            try:
                info = fr.evaluate(_OBJ_PROBE_JS)
            except Exception as e:
                info = {"error": str(e)[:80]}
            if info.get("hasHcaptcha"):
                found = True
                print(f"\n[frame {i}] {url[:90]}")
                print("  " + json.dumps(info, ensure_ascii=False))
        if not found:
            print("\n⚠️ 填卡后（未点 Subscribe）没有任何帧存在 window.hcaptcha")
            print("   → hCaptcha 很可能在点 Subscribe 时才 render；后置 hook 更没戏，必须前置注入。")
    finally:
        close_driver(session)


if __name__ == "__main__":
    main()
