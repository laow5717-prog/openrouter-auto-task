"""hotmail 邮箱数据源 + ruoanzhu 收信服务。

替代 mail.tm 的收码链路：邮箱不是临时新建的，而是来自根目录 `hotmail.xlsx`
（每行 `邮箱----密码----收信链接`）。验证码不走 API，而是从若安(ruoanzhu)的
网页版收信页 HTML 里解析——该页把每封邮件渲染成 email-title / email-content 块。

对外提供两组能力：
1. read_hotmail_accounts(path) -> [HotmailAccount]     解析 xlsx
2. fetch_ruoanzhu_emails(link) -> [dict]               拉取并解析收信页所有邮件
   wait_for_github_launch_code_ruoanzhu(link, ...)     轮询等 GitHub launch code

第 2 组与 src/services/email.py 的 wait_for_github_launch_code 同构，可作 drop-in 替换。
"""
import html
import re
import time
from dataclasses import dataclass

from src.config import cfg
from src.utils import http_session, extract_verification_code


# ---- xlsx 数据源 -----------------------------------------------------------

@dataclass
class HotmailAccount:
    email: str
    password: str
    link: str            # ruoanzhu 收信链接（原样保留，含 e/p/h/r/i 参数）
    raw: str             # 原始整行，便于排错


def _split_row(cell: str):
    """把一行 `邮箱----密码----链接` 拆开。分隔符是四个连字符 `----`。

    返回 HotmailAccount 或 None（字段不足/无邮箱时跳过，不抛异常）。
    """
    if not cell:
        return None
    text = str(cell).strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split("----")]
    if len(parts) < 2 or "@" not in parts[0]:
        return None
    email = parts[0]
    password = parts[1] if len(parts) > 1 else ""
    link = parts[2] if len(parts) > 2 else ""
    return HotmailAccount(email=email, password=password, link=link, raw=text)


def read_hotmail_accounts(path):
    """读取 hotmail.xlsx，返回 [HotmailAccount]。

    遍历所有 sheet 的每个单元格，凡能拆出邮箱的行都收下（数据可能只在 Sheet1，
    但多 sheet 容错更稳）。openpyxl 只读模式，按行取值。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    accounts = []
    seen = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                acc = _split_row(cell)
                if acc and acc.email not in seen:
                    seen.add(acc.email)
                    accounts.append(acc)
    wb.close()
    return accounts


# ---- ruoanzhu 收信页解析 ---------------------------------------------------

# 收信页把每封邮件渲染为：
#   <div class="email-title flex">
#       <b><span ...>SUBJECT</span> ...</b>
#       <span class="time">2026-07-25 03:34:54</span>
#   </div>
#   <div class="email-content" ...>BODY</div>
_TITLE_RE = re.compile(
    r'<div class="email-title[^"]*">(?P<title>.*?)</div>', re.DOTALL)
_TITLE_TEXT_RE = re.compile(r'<b>(?P<inner>.*?)</b>', re.DOTALL)
_TIME_RE = re.compile(r'<span class="time">(?P<time>.*?)</span>', re.DOTALL)
_CONTENT_RE = re.compile(
    r'<div class="email-content"[^>]*>(?P<body>.*?)</div>', re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(fragment):
    """去标签 + 反转义 + 折叠空白，得到纯文本。"""
    if not fragment:
        return ""
    text = _TAG_RE.sub(" ", fragment)
    text = html.unescape(text)
    text = text.replace("\\r", " ").replace("\\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _ruoanzhu_getcontent_url(link):
    """把 xlsx 里的 `/s?e=..&p=..&h=1&r=1&i=2` 链接映射到 getContent API。

    参数映射：e→email, p→pwd, h→htmlOut, r→route, i→islj。
    页面 JS 里「加载最新邮件」按钮指向的正是这个端点，触发实时拉信更可靠。
    映射失败（缺 e/p）时返回原链接兜底。
    """
    m_e = re.search(r'[?&]e=([^&]+)', link)
    m_p = re.search(r'[?&]p=([^&]+)', link)
    if not (m_e and m_p):
        return link
    email = m_e.group(1)
    pwd = m_p.group(1)
    h = (re.search(r'[?&]h=([^&]+)', link) or [None, '1'])[1]
    r = (re.search(r'[?&]r=([^&]+)', link) or [None, '1'])[1]
    i = (re.search(r'[?&]i=([^&]+)', link) or [None, '2'])[1]
    return ("https://www.ruoanzhu.com/tools/emailApi2/getContent"
            f"?email={email}&pwd={pwd}&htmlOut={h}&route={r}&islj={i}")


def fetch_ruoanzhu_html(link, use_api=True):
    """拉取 ruoanzhu 收信页 HTML。use_api=True 时改用 getContent 端点触发实时拉信。

    返回 HTML 字符串或 None（网络/HTTP 失败不抛异常）。
    """
    url = _ruoanzhu_getcontent_url(link) if use_api else link
    try:
        resp = http_session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=cfg.retry.http_timeout,
        )
        if resp.status_code == 200:
            return resp.text
        print(f"  ruoanzhu 收信 HTTP {resp.status_code}")
    except Exception as e:
        print(f"  ruoanzhu 收信失败: {type(e).__name__}: {str(e)[:120]}")
    return None


def parse_ruoanzhu_emails(html_text):
    """从收信页 HTML 解析所有邮件，返回 [{subject, time, body}]（按页面顺序，通常最新在前）。

    标题块与正文块在 HTML 里成对交替出现；分别按顺序抽取后 zip 对齐。
    正文数量可能与标题不等（页面偶发结构差异），以较短者对齐，多余忽略。
    """
    if not html_text:
        return []

    subjects, times = [], []
    for m in _TITLE_RE.finditer(html_text):
        block = m.group("title")
        inner = _TITLE_TEXT_RE.search(block)
        subjects.append(_strip_html(inner.group("inner") if inner else block))
        tm = _TIME_RE.search(block)
        times.append(_strip_html(tm.group("time")) if tm else "")

    bodies = [_strip_html(m.group("body")) for m in _CONTENT_RE.finditer(html_text)]

    emails = []
    for idx in range(min(len(subjects), len(bodies))):
        emails.append({
            "subject": subjects[idx],
            "time": times[idx] if idx < len(times) else "",
            "body": bodies[idx],
        })
    return emails


def fetch_ruoanzhu_emails(link, use_api=True):
    """拉取并解析：返回收信页里的所有邮件 [{subject, time, body}]。"""
    return parse_ruoanzhu_emails(fetch_ruoanzhu_html(link, use_api=use_api))


def extract_github_code_from_emails(emails):
    """从邮件列表里找 GitHub 验证码。

    过滤规则：主题或正文里出现 'github'（大小写不敏感）。GitHub 注册确认邮件
    主题形如 "Your GitHub launch code"，正文/主题含 8 位数字码。命中后先从主题
    提码，取不到再从正文提。返回 (code, matched_email) 或 (None, None)。
    """
    for em in emails:
        subject = em.get("subject", "") or ""
        body = em.get("body", "") or ""
        blob = f"{subject}\n{body}".lower()
        if "github" not in blob:
            continue
        code = extract_verification_code(subject) or extract_verification_code(body)
        if code:
            return code, em
    return None, None


def wait_for_github_launch_code_ruoanzhu(link, timeout=None, poll_interval=None):
    """轮询 ruoanzhu 收信页，等 GitHub launch code。

    与 email.py::wait_for_github_launch_code 同构的 drop-in 替换，签名不同：
    这里用收信链接而非 mail.tm token，且不做 since_ts 时间过滤——hotmail 是真实
    长期邮箱，可能有历史邮件。调用方须保证发信前收件箱内没有会误命中的旧 GitHub
    码（本项目内每个 hotmail 首次注册使用，风险低）；如需更强隔离，后续可加时间过滤。

    返回 str | None。
    """
    if timeout is None:
        timeout = cfg.email.wait_timeout
    if poll_interval is None:
        poll_interval = cfg.email.poll_interval

    print(f"等待 GitHub 验证码邮件（ruoanzhu，最长 {timeout} 秒）...")
    start = time.time()
    while time.time() - start < timeout:
        emails = fetch_ruoanzhu_emails(link) or []
        code, matched = extract_github_code_from_emails(emails)
        if code:
            print(f"\n收到 GitHub 验证码: {code}（邮件主题: {matched.get('subject','')[:60]}）")
            return code
        elapsed = int(time.time() - start)
        print(f"  等待中... ({elapsed}秒, 已解析 {len(emails)} 封)", end="\r")
        time.sleep(poll_interval)

    print("\n等待 GitHub 验证码邮件超时")
    return None
