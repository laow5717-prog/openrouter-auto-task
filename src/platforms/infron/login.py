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


def wait_past_turnstile(session, monitor=None, timeout=90):
    """等 Cloudflare 质询页放行。放行返回页面快照，超时返回 None。

    **只有 AdsPower 指纹环境能过这一关**——实测 Patchright 持久 profile 会永远停在
    质询页。所以超时不要当成「网络慢，重试一次」：重试只会再撞一次同样的墙。上层应当
    直接失败并在 detail 里点明可能是浏览器栈不对，否则现象（页面一直是
    "Just a moment..."）看起来像网络问题，能查很久。

    实测 AdsPower 下约 30 秒自动放行，故默认给到 90 秒。
    """
    deadline = time.time() + timeout
    notified = False
    while time.time() < deadline:
        page = _read(session)
        if page and not _is_turnstile(page) and page['interactive'] > 0:
            return page
        if not notified:
            _step(monitor, session, '等待 Cloudflare 验证页放行')
            notified = True
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
