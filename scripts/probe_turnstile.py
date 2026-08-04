#!/usr/bin/env python3
"""探 infron.ai/login 的 Cloudflare Turnstile 真实结构。

写这个脚本的直接原因：自动勾选没生效。我按「frame 里能读到 verify you are human」
来判断是否需要点击，但那是**猜的**——挂件内容很可能在 shadow DOM 里，
inner_text 读出来是空的，于是判定永远为假、压根不会去点。

所以这次不猜，把真实结构 dump 出来：
  - 所有 frame 的 URL 与可读文本
  - 每个 frame 里 checkbox / input / label 的实际存在情况
  - 挂件 iframe 元素在主文档里的包围盒
  - shadow root 的开合状态（决定选择器能不能穿透）

用法:
    python3 scripts/probe_turnstile.py --email briced35@hotmail.com
    python3 scripts/probe_turnstile.py --email x@y.com --click   # 顺便试着点一下
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import cfg                                     # noqa: E402
from src.models.database import Database                       # noqa: E402
from src.models.adspower_profile import AdsPowerProfileModel    # noqa: E402
from src.services.adspower import AdsPowerClient                # noqa: E402
from src.browser.adspower_driver import (                       # noqa: E402
    AdsPowerProfilePool, create_driver_adspower,
)
from src.browser.driver import close_driver                     # noqa: E402

LOGIN_URL = "https://infron.ai/login"

# 在每个 frame 里跑，摸清它有什么可点的东西。
# 关键是 shadow root：Playwright 的 CSS 选择器只穿 open shadow DOM，
# closed 的话就只能按坐标点了。
FRAME_JS = """
() => {
  const q = s => [...document.querySelectorAll(s)];
  const shadows = q('*').filter(e => e.shadowRoot).map(e => ({
    tag: e.tagName.toLowerCase(),
    mode: 'open',                       // 能拿到 shadowRoot 就是 open
    inner: (e.shadowRoot.textContent || '').trim().slice(0, 120),
    inputs: [...e.shadowRoot.querySelectorAll('input')].map(i => i.type),
  }));
  return {
    url: location.href,
    title: document.title || '',
    text: (document.body ? document.body.innerText : '').trim().slice(0, 300),
    html_head: (document.body ? document.body.innerHTML : '').trim().slice(0, 600),
    checkboxes: q("input[type='checkbox']").length,
    inputs: q('input').map(i => ({type: i.type, id: i.id, name: i.name})),
    labels: q('label').map(l => (l.innerText || '').trim().slice(0, 60)),
    buttons: q('button').map(b => (b.innerText || '').trim().slice(0, 60)),
    shadow_hosts: shadows,
  };
}
"""


def dump(session, label):
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    page = session.page
    print(f"主文档 URL: {page.url}")
    try:
        print(f"主文档标题: {page.title()}")
    except Exception:
        pass

    for i, fr in enumerate(page.frames):
        try:
            info = fr.evaluate(FRAME_JS)
        except Exception as e:
            print(f"\n[frame {i}] {fr.url[:90]}\n    读取失败: {type(e).__name__}: {str(e)[:80]}")
            continue
        print(f"\n[frame {i}] {info['url'][:100]}")
        print(f"    title      = {info['title']!r}")
        print(f"    text       = {info['text']!r}")
        print(f"    checkbox   = {info['checkboxes']}   inputs = {info['inputs']}")
        print(f"    labels     = {info['labels']}")
        print(f"    buttons    = {info['buttons']}")
        if info['shadow_hosts']:
            print(f"    shadowRoot（open，选择器可穿透）:")
            for sh in info['shadow_hosts']:
                print(f"        <{sh['tag']}> inputs={sh['inputs']} text={sh['inner']!r}")
        else:
            print(f"    shadowRoot = 无 open shadow root"
                  f"（若挂件明明有内容却读不到，说明是 closed，只能按坐标点）")
        if 'challenges.cloudflare.com' in info['url']:
            print(f"    ── 这是 Turnstile 挂件帧 ──")
            print(f"    html 前 600 字: {info['html_head']!r}")

    # 挂件 iframe 在主文档里的位置，坐标兜底要用
    try:
        holder = page.locator("iframe[src*='challenges.cloudflare.com']").first
        if holder.count() > 0:
            print(f"\n挂件 iframe 包围盒: {holder.bounding_box()}")
        else:
            print("\n主文档里找不到 challenges.cloudflare.com 的 iframe 元素")
    except Exception as e:
        print(f"\n取包围盒失败: {type(e).__name__}: {str(e)[:80]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--email', required=True)
    ap.add_argument('--click', action='store_true', help='试着调用现有的勾选函数')
    ap.add_argument('--wait', type=int, default=8, help='打开后先等几秒再 dump')
    ap.add_argument('--keep', action='store_true', help='跑完不关浏览器')
    args = ap.parse_args()

    db = Database(str(__import__('src.config', fromlist=['get_data_dir'])
                      .get_data_dir() / 'openrouter_auto.db'))
    client = AdsPowerClient(cfg.adspower.base_url, cfg.adspower.api_key)
    pool = AdsPowerProfilePool(client, AdsPowerProfileModel(db),
                               group_id=cfg.adspower.group_id,
                               reclaim_batch=cfg.adspower.reclaim_batch,
                               ua_systems=cfg.adspower.ua_systems,
                               log=print)

    session = create_driver_adspower(args.email, pool, client)
    try:
        print(f"打开 {LOGIN_URL} …")
        session.get(LOGIN_URL)
        time.sleep(args.wait)
        dump(session, f"打开后 {args.wait} 秒")

        if args.click:
            from src.platforms.infron.login import (
                click_turnstile_checkbox, _turnstile_frame, _needs_click,
            )
            fr = _turnstile_frame(session)
            print(f"\n_turnstile_frame 找到帧: {fr.url[:80] if fr else None}")
            if fr is not None:
                print(f"_needs_click 判定: {_needs_click(fr)}   ← False 就不会去点")
            got = click_turnstile_checkbox(session)
            print(f"click_turnstile_checkbox 返回: {got}")
            time.sleep(6)
            dump(session, "点击尝试后 6 秒")

        # 再等一会儿看是否自动放行
        time.sleep(20)
        dump(session, "再等 20 秒后")
    finally:
        if not args.keep:
            close_driver(session)


if __name__ == '__main__':
    main()
