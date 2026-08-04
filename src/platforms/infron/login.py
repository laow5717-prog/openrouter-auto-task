"""infron.ai 会话建立：Cloudflare Turnstile 等待 + 邮箱 magic link 登录。

比 opencode 简单得多——没有 GitHub OAuth 链、没有新设备验证、没有密码。填邮箱、
收一封信、打开里面的链接就完事，而且**首次登录会自动建号**，注册与登录是同一条路。

代价是两处必须小心的地方，见 `wait_past_turnstile` 与 `_find_magic_link` 的注释。
"""

import re
import time
from datetime import datetime, timedelta

from src.browser.monitor import step as _step
from src.services.hotmail_inbox import fetch_ruoanzhu_emails, parse_mail_time

LOGIN_URL = 'https://infron.ai/login'
DASHBOARD_URL = 'https://infron.ai/dashboard'

# 邮件里的登录链接。一次性、30 分钟有效。
_MAGIC_RE = re.compile(r'https://infron\.ai/api/user/magic-link/verify\?token=[0-9a-fA-F-]+')

# 主题过滤词。实测主题是 'Infron - Sign In Link-Infron'。
_MAIL_SUBJECT_HINT = 'infron'

# 收信页时间与本机时钟的容差（秒）。与 hotmail_inbox 用同一个理由：卡太死会把刚到的
# 新邮件判成旧邮件而永远收不到。
_MAIL_TIME_TOLERANCE_SEC = 90

# Cloudflare 质询页的特征
_TURNSTILE_TITLE = 'just a moment'
_TURNSTILE_BODY_HINTS = ('performing security verification',
                         'verifies you are not a bot')

# Turnstile 挂件所在的 iframe。
#
# ⚠️ 实探（scripts/probe_turnstile.py，2026-08-04）确认的两件事，都与直觉相反：
#
# 1. **挂件的 iframe 在 closed shadow DOM 里**。`document.querySelectorAll('iframe')`
#    完全看不到它，所以 `page.locator("iframe[src*=...]")` 永远返回 0 个元素。
#    要拿到这个元素只能走 `frame.frame_element()`——Playwright 的 frame 树不受
#    shadow DOM 影响。
# 2. **挂件帧的内容也读不出来**：`fr.inner_text('body')` 返回空串，
#    `document.body.innerHTML` 也是空的（内层还有一层 closed shadow root）。
#    所以任何「读文案判断要不要点」的方案都不成立。
_TURNSTILE_FRAME_MARK = 'challenges.cloudflare.com'

# 被动形态自己放行需要多久。实测约 34 秒（标题 'Checking your Browser…'，
# 全页面零个 checkbox）。**在这之前绝不能去点**——被动挑战被打断可能反而重置。
_TURNSTILE_PASSIVE_GRACE_SEC = 40

# 交互式复选框挂件的最小宽度。Cloudflare 的标准复选框挂件约 300×65；
# 被动全页质询的挂件是隐藏的（尺寸为 0 或不可见）。
# 用**可见尺寸**而不是文案来区分两种形态，因为文案根本读不到（见上）。
_TURNSTILE_MIN_WIDGET_W = 120

# 两次点击之间的最短间隔。Turnstile 点完要几秒才出结果，连点既没用又像机器人。
_TURNSTILE_CLICK_GAP_SEC = 8

_PAGE_JS = """
() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return {
    url: location.href,
    title: document.title || '',
    body: (document.body ? document.body.innerText : '') || '',
    interactive: [...document.querySelectorAll('button,a,input')].filter(vis).length,
  };
}
"""


def _read(session):
    try:
        return session.page.evaluate(_PAGE_JS)
    except Exception:
        return None


def _is_turnstile(page):
    if not page:
        return False
    if _TURNSTILE_TITLE in (page['title'] or '').lower():
        return True
    body = (page['body'] or '').lower()
    return any(h in body for h in _TURNSTILE_BODY_HINTS)


def _turnstile_frame(session):
    """找 Turnstile 挂件的 frame。没有返回 None。"""
    try:
        for fr in session.page.frames:
            if _TURNSTILE_FRAME_MARK in (fr.url or ''):
                return fr
    except Exception:
        pass
    return None


def _widget_box(fr):
    """挂件在页面上的可见包围盒。拿不到返回 None。

    必须走 `frame.frame_element()`——挂件的 iframe 在 closed shadow DOM 里，
    `document.querySelectorAll('iframe')` 看不到它，因此
    `page.locator("iframe[src*=...]")` 恒返回 0 个元素（实探确认）。
    Playwright 的 frame 树不受 shadow DOM 影响，这是唯一能拿到该元素的入口。
    """
    try:
        el = fr.frame_element()
        if not el.is_visible():
            return None
        return el.bounding_box()
    except Exception:
        return None


def click_turnstile_checkbox(session, monitor=None):
    """点一下 Turnstile 的交互式复选框。点到了返回 True。

    **只处理交互形态。** 判据是挂件有可见且够宽的包围盒——不能靠读文案，
    挂件内容在 closed shadow root 里，`inner_text` 恒为空串（实探确认）。
    被动全页质询的挂件不可见/尺寸为 0，因此不会被误点。

    调用方负责在被动放行的宽限期之后才调它，见 `wait_past_turnstile`。

    两条路径：frame 内直接点（能命中就最好），失败则按包围盒坐标点——
    复选框固定在挂件左端约 30px 处。坐标兜底看着糙，但 closed shadow DOM 下
    选择器穿不透，没有它交互式挑战完全无解。
    """
    fr = _turnstile_frame(session)
    if fr is None:
        return False

    box = _widget_box(fr)
    if not box or box['width'] < _TURNSTILE_MIN_WIDGET_W:
        # 记一笔尺寸：这是下次遇到交互形态时唯一能拿来校准的数据。
        _step(monitor, session,
              f'Turnstile 挂件不可点（box={box}），继续等待自动放行')
        return False

    # 路径 1：frame 内直接点
    for sel in ("input[type='checkbox']", "#challenge-stage input",
                "label[for]", ".cb-lb input", "#checkbox"):
        try:
            loc = fr.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=3000)
                _step(monitor, session, '已勾选 Turnstile「确认是真人」')
                return True
        except Exception:
            continue

    # 路径 2：按包围盒坐标点
    try:
        session.page.mouse.click(box['x'] + 30, box['y'] + box['height'] / 2)
        _step(monitor, session,
              f"已按坐标点击 Turnstile 复选框（box={box['width']:.0f}×{box['height']:.0f}）")
        return True
    except Exception:
        return False


def wait_past_turnstile(session, monitor=None, timeout=90):
    """等 Cloudflare 质询页放行。放行返回页面快照，超时返回 None。

    **只有 AdsPower 指纹环境能过这一关**——实测 Patchright 持久 profile 会永远停在
    质询页。所以超时不要当成「网络慢，重试一次」：重试只会再撞一次同样的墙。上层应当
    直接失败并在 detail 里点明可能是浏览器栈不对，否则现象（页面一直是
    "Just a moment..."）看起来像网络问题，能查很久。

    实测 AdsPower 下约 30 秒自动放行，故默认给到 90 秒。

    ⚠️ Turnstile 有两种形态，处置方式相反：

    - **被动**（实测常见）：标题 'Checking your Browser…'，全页面零个 checkbox，
      约 34 秒自己放行。这段时间**什么都不要做**——去点它反而可能重置挑战。
    - **交互**：渲染一个复选框等人点，不点就永远不过。

    所以顺序是「先等足 _TURNSTILE_PASSIVE_GRACE_SEC，仍未放行才考虑点」。
    是否可点由挂件的可见包围盒判定，不是读文案——挂件内容在 closed shadow root 里，
    读出来恒为空串（实探确认，见 scripts/probe_turnstile.py）。
    """
    started = time.time()
    deadline = started + timeout
    notified = False
    last_click = 0.0
    while time.time() < deadline:
        page = _read(session)
        if page and not _is_turnstile(page) and page['interactive'] > 0:
            return page
        if not notified:
            _step(monitor, session, '等待 Cloudflare 验证页放行')
            notified = True

        # 宽限期内绝不打扰：被动形态本来就会自己过，插一脚只会坏事。
        waited = time.time() - started
        if waited >= _TURNSTILE_PASSIVE_GRACE_SEC and \
                time.time() - last_click >= _TURNSTILE_CLICK_GAP_SEC:
            last_click = time.time()      # 无论点没点到都计时，避免每轮都去探
            try:
                click_turnstile_checkbox(session, monitor)
            except Exception:
                pass      # 点不到就继续等，不能让它把整个登录搞挂

        time.sleep(3)
    return None


def _find_magic_link(emails, since):
    """从收信页邮件里找**本次发起之后**到达的 magic link。

    时间闸门不能省：收件箱里往往还留着上一轮的链接，而那些链接要么已用过、要么已过期
    （一次性 + 30 分钟）。误用旧链接的表现是「登录莫名其妙失败」，且每次都能稳定复现
    ——极难往「用错了链接」这个方向想。与 GitHub 收码的闸门是同一个道理。

    时间比较带 90 秒容差，吸收收信服务与本机的时钟漂移。
    """
    cutoff = since - timedelta(seconds=_MAIL_TIME_TOLERANCE_SEC) if since else None
    for mail in emails or []:
        subject = (mail.get('subject') or '').lower()
        body = mail.get('body') or ''
        if _MAIL_SUBJECT_HINT not in subject and _MAIL_SUBJECT_HINT not in body.lower():
            continue
        if cutoff is not None:
            arrived = parse_mail_time(mail.get('time'))
            if arrived is None or arrived < cutoff:
                continue          # 时间不可信或是旧邮件，一律跳过
        hit = _MAGIC_RE.search(body)
        if hit:
            return hit.group(0), mail
    return None, None


def wait_for_magic_link(verify_link, since, monitor=None, session=None, timeout=120,
                        poll_interval=6):
    """轮询 ruoanzhu 收信页取 magic link。取不到返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            emails = fetch_ruoanzhu_emails(verify_link)
        except Exception as e:
            _step(monitor, session, f'收信失败（将重试）：{str(e)[:80]}')
            emails = []
        link, mail = _find_magic_link(emails, since)
        if link:
            _step(monitor, session, f"收到登录链接（邮件时间 {mail.get('time')}）")
            return link
        time.sleep(poll_interval)
    return None


def is_logged_in(page):
    """当前页面是否已在控制台内。"""
    return bool(page) and '/dashboard' in (page['url'] or '')


def ensure_session(session, creds, monitor=None, timeout=240):
    """确保处于登录态。返回 (ok, detail)。

    主路径是**复用已登录环境**，不是每次都发信：AdsPower 环境按邮箱持久，cookie 就在
    里面。每轮都走一遍 magic link 既慢（要等收信）又白白消耗一次性链接，还可能撞上
    发信频控。
    """
    session.get(DASHBOARD_URL)
    page = wait_past_turnstile(session, monitor, timeout=90)
    if page is None:
        return False, ('Cloudflare 验证页未放行——infron 必须用 AdsPower 指纹环境，'
                       'Patchright/本地 Chrome 过不了这一关')

    if is_logged_in(page):
        _step(monitor, session, '已登录（复用环境登录态）')
        return True, '已登录（复用环境登录态）'

    if not creds.verify_link:
        return False, '该账号无收信链接（accounts.email_verify_link 为空），无法收 magic link'

    # 时间下界必须在**点 Sign In 之前**取：站点是在提交那一刻发信的，取晚了会把刚到的
    # 新邮件判成旧邮件而永远收不到。
    since = datetime.now()

    session.get(LOGIN_URL)
    page = wait_past_turnstile(session, monitor, timeout=90)
    if page is None:
        return False, 'Cloudflare 验证页未放行（登录页）'

    _step(monitor, session, f'填邮箱 {creds.email}，请求登录链接')
    try:
        session.page.fill('#email', creds.email)
        time.sleep(1)
        session.page.get_by_role('button', name='Sign In', exact=True).first.click(timeout=15000)
    except Exception as e:
        return False, f'提交登录表单失败：{type(e).__name__}: {str(e)[:120]}'

    time.sleep(5)
    link = wait_for_magic_link(creds.verify_link, since, monitor, session, timeout=120)
    if not link:
        return False, '120 秒内未收到 infron 登录链接'

    _step(monitor, session, '打开登录链接')
    session.get(link)
    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(3)
        page = _read(session)
        if is_logged_in(page):
            _step(monitor, session, '登录成功，已进入控制台')
            return True, '经 magic link 登录成功'
    return False, '打开登录链接后未落地控制台（链接可能已过期或被用过）'
