"""
Flask API 路由
所有 /api/* 端点
"""

import os
import io
import json
from flask import Blueprint, jsonify, request, send_from_directory, send_file

from src.config import cfg, get_data_dir
from src.services import card as card_service
from src.services import registration
from src.models.card_pool import CardPoolModel
from src.utils import is_card_expired, is_identity_terminal, is_platform_terminal

api = Blueprint('api', __name__)


def get_app_state():
    """默认平台的运行上下文。

    ⚠️ 多平台并发后，「AppState」已经不是全局唯一的了——每个平台一个。
    凡是与**某次运行**有关的操作（启动/停止/看状态/看日志），都必须用
    `get_ctx()` 按平台取，用这个函数会永远操作默认平台。

    保留它是因为有一批接口只碰共享资源（models、db、registry），
    用哪个 ctx 都一样；那些地方继续用它没问题。
    """
    from flask import current_app
    return current_app.config['APP_STATE']


def get_contexts():
    """全部平台的运行上下文，{slug: ctx}。"""
    from flask import current_app
    return current_app.config.get('RUN_CONTEXTS') or {
        current_app.config['APP_STATE'].platform: current_app.config['APP_STATE']
    }


def get_ctx(platform=None):
    """取某个平台的运行上下文。platform 为空时用请求里的平台参数。

    找不到对应平台时回落到默认 ctx 而不是报错：平台列表由代码里的适配器注册表
    决定，传了未知 slug 属于调用方的问题，但让整个接口 500 更难查。
    """
    ctxs = get_contexts()
    slug = platform or _req_platform()
    return ctxs.get(slug) or get_app_state()


def get_db():
    from flask import current_app
    return current_app.config['DB']


def get_models():
    from flask import current_app
    return current_app.config['MODELS']


def _req_platform(required=False):
    """取本次请求的目标平台 slug。

    读接口（账号/卡池列表）缺省时回落到**默认平台**，让不带参数的老调用仍能工作。
    写接口与卡池类接口应传 required=True——猜错平台会返回或写入混合数据，
    那比直接报错糟糕得多。

    兜底值曾经是「AppState.platform」，也就是**上一次运行的那个平台**。单平台时
    它恰好等于用户正在看的平台，所以一直没出事；两个平台同时跑之后，这个值会被
    后启动的那个覆盖，于是不带参数的请求会随机地读到另一个平台的数据。
    现在改成固定的默认平台常量——猜得稳定，总比猜得随机好。
    """
    value = (request.args.get('platform')
             or (request.get_json(silent=True) or {}).get('platform')
             or '')
    value = value.strip()
    if value:
        return value
    if required:
        return None
    from src.web.app import AppState
    return AppState.DEFAULT_PLATFORM


@api.route('/api/platforms')
def list_platforms():
    """可用平台列表 + 当前选中平台。

    真值源是代码里的适配器注册表，不是数据库——平台随代码发布变化，不是运行时数据。
    """
    import src.platforms as platforms
    return jsonify({
        "data": platforms.describe_all(),
        # 「当前」的语义已经变了：多平台并发时没有唯一的当前平台。这里返回**默认**
        # 平台，仅供前端首次加载时选一个；真正在看哪个由前端 store 自己持有。
        "current": get_app_state().platform,
        "running": [slug for slug, c in get_contexts().items() if c.is_running],
    })


@api.route('/api/status')
def get_status():
    """全局状态 + 各 worker 概览。

    向后兼容：顶层字段全部保留原语义，workers 数组是新增的旁挂字段。
    串行运行时 workers 只有 W1，顶层 current_action 即 W1 的动作。

    多平台：顶层字段是**所请求平台**的状态（platform 参数，缺省用默认平台）。
    另外旁挂一个 `platforms` 汇总，让前端能看见**没在看的那个平台**是否在跑——
    否则它出问题时用户完全看不见。
    """
    state = get_ctx()
    models = get_models()

    total_inventory = models['account'].count()

    return jsonify({
        "platform": state.platform,
        "is_running": state.is_running,
        "current_action": state.current_action,
        "success": state.success_count,
        "fail": state.fail_count,
        "total_inventory": total_inventory,
        "logs": state.get_logs(int(request.args.get('log_index', 0))),
        "parallel_mode": state.parallel_mode,
        "workers": [
            {
                "id": w.worker_id,
                "current_action": w.current_action,
                "busy": w.busy,
                "log_seq": w.log_seq,
            }
            for w in state.active_workers()
        ],
        # 全平台概览。刻意只放很轻的字段——它每秒被轮询一次。
        "platforms": {
            slug: {
                "is_running": c.is_running,
                "current_action": c.current_action,
                "success": c.success_count,
                "fail": c.fail_count,
            }
            for slug, c in get_contexts().items()
        },
        "quota": state.quota.snapshot() if hasattr(state, 'quota') else None,
    })


@api.route('/api/workers/<worker_id>/logs')
def get_worker_logs(worker_id):
    """按 worker 增量拉取日志。

    index 传上次返回的 next_index；worker 不存在时回落到主 worker，
    保证前端在 worker 数变化时不会拿到 404。

    必须按平台取 ctx：两个平台各有一套同名的 W1..W4，取错 ctx 会拿到
    另一个平台那个 worker 的日志——而且因为有「回落主 worker」的兜底，
    不会 404，只会安静地给错数据。
    """
    state = get_ctx()
    worker = state.get_worker(worker_id)
    logs, next_index = worker.get_logs(int(request.args.get('index', 0)))
    return jsonify({
        "worker_id": worker.worker_id,
        "logs": logs,
        "next_index": next_index,
        "current_action": worker.current_action,
        "busy": worker.busy,
    })


@api.route('/api/start', methods=['POST'])
def start_task():
    state = get_ctx()
    if state.is_running:
        return jsonify({"error": f"{state.platform} 已有任务在运行"}), 400

    data = request.json or {}
    count = data.get('count', 1)
    card_info_list = data.get('card_info_list', None)
    login_password = data.get('login_password', None)
    max_bindable_cards = data.get('max_bindable_cards', 2)
    captcha_api_key = data.get('captcha_api_key', None)

    import threading
    threading.Thread(
        target=state.run_batch_task,
        args=(count, card_info_list, login_password, max_bindable_cards, captcha_api_key),
        daemon=True,
    ).start()

    return jsonify({"status": "started"})


@api.route('/api/stop', methods=['POST'])
def stop_task():
    """停止**某一个平台**的任务，不影响另一个平台（AC2）。"""
    state = get_ctx()
    if not state.is_running:
        return jsonify({"error": f"{state.platform} 没有正在运行的任务"}), 400

    state.force_stop()
    return jsonify({"status": "stopping", "platform": state.platform})


@api.route('/api/card/template')
def download_card_template():
    template_path = card_service.generate_template()
    directory = os.path.dirname(template_path)
    filename = os.path.basename(template_path)
    return send_from_directory(directory, filename, as_attachment=True)


@api.route('/api/card/history')
def get_card_history():
    models = get_models()

    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    status = request.args.get('status', '')
    keyword = request.args.get('keyword', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    records, total = models['card_binding'].get_all_paginated(
        page=page, page_size=page_size,
        status=status, keyword=keyword,
        date_from=date_from, date_to=date_to,
    )
    summary = models['card_binding'].get_global_summary()

    formatted = []
    for r in records:
        card = {}
        if r.get('card_data_json'):
            try:
                card = json.loads(r['card_data_json'])
            except (json.JSONDecodeError, TypeError):
                pass
        formatted.append({
            "task_id": r.get('task_id'),
            "card_number": card.get('number', ''),
            "card_holder": f"{card.get('first_name', '')} {card.get('last_name', '')}".strip(),
            "expiry_month": card.get('expiry_month', ''),
            "expiry_year": card.get('expiry_year', ''),
            "cvc": card.get('cvc', ''),
            "country": card.get('country', ''),
            "address": card.get('address', ''),
            "address2": card.get('address2', ''),
            "city": card.get('city', ''),
            "state": card.get('state', ''),
            "zip": card.get('zip', ''),
            "company": card.get('company', ''),
            "status": r['status'],
            "bound_to_email": r.get('bound_to_email') or '',
            "error": r.get('error') or '',
            "attempted_at": r.get('attempted_at') or '',
        })

    return jsonify({
        "data": formatted,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": summary,
    })


@api.route('/api/card/history/cleanup', methods=['POST'])
def cleanup_card_history():
    models = get_models()
    # 保护**所有**平台正在跑的任务，不只当前这个——多平台并发时可能同时有两个
    # 批量任务在跑，只保护其中一个会把另一个的绑卡记录删掉。
    active = [c.current_card_task_id for c in get_contexts().values()
              if c.is_running and c.current_card_task_id]
    deleted = models['card_binding'].cleanup_stale_pending(active)
    return jsonify({"deleted": deleted})


@api.route('/api/card/history/export', methods=['POST'])
def export_card_history():
    models = get_models()
    data = request.json or {}

    status = data.get('status', '')
    keyword = data.get('keyword', '')
    date_from = data.get('date_from', '')
    date_to = data.get('date_to', '')

    records = models['card_binding'].get_all_filtered(
        status=status, keyword=keyword,
        date_from=date_from, date_to=date_to,
    )

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "绑卡记录"

    headers = ["序号", "卡号", "状态", "绑定账号", "错误信息", "处理时间", "任务批次",
               "完整卡号", "有效期", "CVC", "持卡人",
               "国家", "地址1", "地址2", "城市", "州/省", "邮编", "公司"]
    header_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    header_font = Font(bold=True, size=11)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font

    for row_idx, r in enumerate(records, 2):
        card = {}
        if r.get('card_data_json'):
            try:
                card = json.loads(r['card_data_json'])
            except (json.JSONDecodeError, TypeError):
                pass

        ws.cell(row=row_idx, column=1, value=r['id'])
        # card_bindings.card_display 存的是后四位，优先用 card_data_json 里的完整卡号
        ws.cell(row=row_idx, column=2, value=card.get('number') or r['card_display'])
        ws.cell(row=row_idx, column=3, value=r['status'])
        ws.cell(row=row_idx, column=4, value=r.get('bound_to_email') or '')
        ws.cell(row=row_idx, column=5, value=r.get('error') or '')
        ws.cell(row=row_idx, column=6, value=r.get('attempted_at') or '')
        ws.cell(row=row_idx, column=7, value=r.get('task_id') or '')
        ws.cell(row=row_idx, column=8, value=card.get('number', ''))
        expiry = f"{card.get('expiry_month', '')}/{card.get('expiry_year', '')}" if card.get('expiry_month') else ''
        ws.cell(row=row_idx, column=9, value=expiry)
        ws.cell(row=row_idx, column=10, value=card.get('cvc', ''))
        ws.cell(row=row_idx, column=11, value=f"{card.get('first_name', '')} {card.get('last_name', '')}".strip())
        ws.cell(row=row_idx, column=12, value=card.get('country', ''))
        ws.cell(row=row_idx, column=13, value=card.get('address', ''))
        ws.cell(row=row_idx, column=14, value=card.get('address2', ''))
        ws.cell(row=row_idx, column=15, value=card.get('city', ''))
        ws.cell(row=row_idx, column=16, value=card.get('state', ''))
        ws.cell(row=row_idx, column=17, value=card.get('zip', ''))
        ws.cell(row=row_idx, column=18, value=card.get('company', ''))

    for col in ws.columns:
        max_len = 0
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='card_history_export.xlsx',
    )


@api.route('/api/accounts')
def get_accounts():
    models = get_models()

    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    keyword = request.args.get('keyword', '')
    identity_status = request.args.get('identity_status', '')
    platform_status = request.args.get('platform_status', '')
    platform = _req_platform()
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    accounts, total = models['account'].get_paginated(
        page=page, page_size=page_size,
        keyword=keyword, identity_status=identity_status,
        date_from=date_from, date_to=date_to,
    )

    emails = [acc['email'] for acc in accounts]
    card_counts = models['card_binding'].count_by_emails(emails)
    pa_map = models['platform_account'].map_by_email(platform, emails)

    # 平台状态过滤只能在这里做：它来自另一张表，塞不进 accounts 的分页 SQL。
    # 代价是分页会与 total 对不齐（本页过滤掉几条，总数仍是身份层的计数）。
    # 这是有意的折中——把平台状态并进分页查询要写成 JOIN + 动态条件，
    # 而这个筛选平时很少用。前端在启用该筛选时提示"总数为身份层计数"。
    if platform_status:
        accounts = [a for a in accounts
                    if (pa_map.get(a['email']) or {}).get('status') == platform_status]

    data = []
    for acc in accounts:
        pa = pa_map.get(acc['email']) or {}
        data.append({
            "email": acc['email'],
            "password": acc.get('login_password') or '',
            # identity_status：GitHub 注册/封禁结果，跨平台一致
            # platform_status：该账号在当前平台的业务状态；'' 表示尚未在此平台开通
            "identity_status": acc.get('identity_status') or '',
            "platform": platform,
            "platform_status": pa.get('status') or '',
            "time": acc.get('created_at') or '',
            "email_password": acc.get('email_password') or '',
            # card_count：库内 card_bindings 成功关联的卡数（可点开看明细）
            "card_count": card_counts.get(acc['email'], 0),
            "credits_balance": pa.get('credits_balance'),
            "balance_updated_at": pa.get('balance_updated_at') or '',
            "apikey": pa.get('apikey') or '',
            "apikey_updated_at": pa.get('apikey_updated_at') or '',
            "tenant_id": pa.get('tenant_id') or '',
            "email_verify_link": acc.get('email_verify_link') or '',
        })

    return jsonify({"data": data, "total": total, "page": page, "page_size": page_size,
                    "platform": platform})


@api.route('/api/accounts/delete', methods=['POST'])
def delete_accounts():
    models = get_models()
    data = request.json or {}
    emails = data.get('emails', [])
    if not emails:
        return jsonify({"error": "没有指定要删除的账号"}), 400

    # 删除关联的卡片绑定记录
    placeholders = ','.join(['?'] * len(emails))
    models['card_binding'].db.execute(
        f"DELETE FROM card_bindings WHERE bound_to_email IN ({placeholders})", emails
    )

    # 身份没了，它在各平台的账号行也就没有意义了——不清会留下引用不存在邮箱的孤儿行
    models['platform_account'].delete_all_for_emails(emails)
    count = models['account'].delete_by_emails(emails)
    return jsonify({"deleted": count})


@api.route('/api/accounts/<email>/cards')
def get_account_cards(email):
    models = get_models()
    cards = models['card_binding'].get_by_email(email)
    return jsonify(cards)


@api.route('/api/accounts/recharge', methods=['POST'])
def recharge_account():
    models = get_models()

    data = request.json or {}
    platform = _req_platform()
    # 按平台取运行上下文——闸门、计数、日志全都是这个 ctx 的。
    # 曾经这里取的是全局单例，于是 infron 的充值会被 opencode 正在跑的任务挡住。
    state = get_ctx(platform)

    if state.is_running:
        return jsonify({"error": f"{platform} 已有任务在运行，请等待完成后再操作"}), 400
    email = data.get('email', '')
    payment_group_id = data.get('payment_group_id')
    captcha_api_key = data.get('captcha_api_key')
    # 充值默认用 Multibot 解支付页 hCaptcha；可传 captcha_server='2captcha.com' 切回
    captcha_server = data.get('captcha_server') or 'api.multibot.cloud'
    if not email:
        return jsonify({"error": "未指定账号"}), 400

    # 查找账号
    rows = models['account'].search(email)
    account = None
    for r in rows:
        if r['email'] == email:
            account = r
            break

    if not account:
        return jsonify({"error": "账号不存在"}), 404

    # 不在这里校验凭据：**需要哪些凭据是平台决定的**。opencode 走 GitHub OAuth 要
    # login_password，infron 走邮箱 magic link 只要 email_verify_link，将来的平台可能
    # 两个都不要。缺什么由 adapter.ensure_session 判断并给出可读的 detail
    # （opencode 会说「该账号未保存登录密码，且 profile 未登录」）。
    #
    # 这个前置校验对 opencode 也偏严：profile 已登录但没存密码的账号本来能跑，
    # 却被挡在门外。
    login_password = account.get('login_password')

    # opencode 充值时直接在 Stripe Checkout 填卡，无需预先绑卡，故不再校验 bound 状态。
    # 支付卡来自 payment_group_id 指定的卡池分组（见 _recharge_one_account → recharge_account）。

    # 不再需要「把 platform 赋给 state」——ctx 本身就是按平台取的，它的 .platform
    # 就是这个值。曾经那行赋值是单例时代的产物，多平台并发下改全局字段正是要消除的
    # 竞态源（两个平台同时跑会互相覆盖）。

    # 标记为运行中，在后台线程执行充值。
    #
    # stop_requested 必须跟着一起复位：它是**跨任务残留**的。上一次任务被用户停止、
    # 或某个 worker 抛 InterruptedError（worker.py 会置全局 stop_requested 让其它
    # worker 一起收敛）之后，这个标志一直是 True；不复位的话，下一次充值会在第一个
    # 检查点就自杀，日志只留一句「收到停止请求，正在中断」——看起来像用户又点了停止，
    # 极难往「上一轮的残留」上想。三条流水线入口都成对复位了，只有这里漏了。
    state.is_running = True
    state.stop_requested = False
    state.current_action = f"正在为 {email} 在 {platform} 充值..."

    import threading

    def _do_recharge():
        # 绑定必须在**本线程内**做：contextvars 不跨线程继承，在请求线程里绑
        # 对这个新线程毫无作用，日志会因为解析不出归属而退化成裸 print。
        # （_patch_prints 同时完成「装钩子」与「绑本线程」，钩子部分是幂等的。）
        state._patch_prints()
        try:
            state._recharge_one_account(email, login_password, payment_group_id,
                                        captcha_api_key=captcha_api_key,
                                        captcha_server=captcha_server)
        except InterruptedError:
            state.add_log("充值已中断")
        except Exception as e:
            state.add_log(f"充值异常: {e}")
        finally:
            state.clear_active_driver()
            state._stop_screenshot_loop()
            state.is_running = False

    threading.Thread(target=_do_recharge, daemon=True).start()
    return jsonify({"status": "started", "email": email})


@api.route('/api/accounts/open-browser', methods=['POST'])
def open_account_browser():
    """打开浏览器查看账号（平台控制台），按账号独立，不阻塞全局任务"""
    models = get_models()
    platform = _req_platform()
    # 按平台取：下游要用这个 ctx 的 browser_factory 与该平台的适配器。
    state = get_ctx(platform)

    data = request.json or {}
    email = data.get('email', '')
    if not email:
        return jsonify({"error": "未指定账号"}), 400

    rows = models['account'].search(email)
    account = None
    for r in rows:
        if r['email'] == email:
            account = r
            break

    if not account:
        return jsonify({"error": "账号不存在"}), 404

    # 不在这里校验凭据：**需要哪些凭据是平台决定的**。opencode 走 GitHub OAuth 要
    # login_password，infron 走邮箱 magic link 只要 email_verify_link，将来的平台可能
    # 两个都不要。缺什么由 adapter.ensure_session 判断并给出可读的 detail
    # （opencode 会说「该账号未保存登录密码，且 profile 未登录」）。
    #
    # 这个前置校验对 opencode 也偏严：profile 已登录但没存密码的账号本来能跑，
    # 却被挡在门外。
    login_password = account.get('login_password')

    # 预约 profile：与任务 worker 的账号占用共用一把锁，避免同一 Chrome profile
    # 被 worker 与手动会话同时使用（会互删 Singleton 锁导致浏览器崩溃）
    ok, reason = state.account_registry.try_open_manual(email)
    if not ok:
        return jsonify({"error": reason}), 409

    state.add_log(f"{email} 正在打开浏览器...")

    import threading

    def _do_open():
        from src.browser.driver import create_driver, close_driver
        import src.platforms as platforms
        from src.platforms.base import Credentials
        adapter = platforms.get(platform)
        driver = None
        try:
            driver = create_driver(headless=False, profile_id=email)

            # 复用充值流程用的同一套会话建立逻辑（adapter.ensure_session）。
            # 遇新设备验证等人工环节，该流程会保持浏览器打开等待人工完成。
            def _monitor(_drv, step):
                if step:
                    state.add_log(f"{email}: {step}")

            sess = adapter.ensure_session(
                driver,
                Credentials(email=email, login_password=login_password,
                            verify_link=account.get('email_verify_link')),
                monitor=_monitor,
            )
            if sess.ok:
                state.add_log(f"{email} 已登录 {adapter.display_name}"
                              f"（{sess.detail}），租户={sess.tenant_id}")
            else:
                state.add_log(f"{email} 未能自动登录 {adapter.display_name}："
                              f"{sess.detail}，请在浏览器手动操作")

            # 等待用户手动关闭浏览器；期间轮询 billing 页 Current Balance，
            # 用户进入结算页时读到即落库刷新余额（driver.title 触发事件派发保活）。
            import time
            last_persisted = None
            while True:
                try:
                    _ = driver.title
                    bal = adapter.read_balance_from_current_page(driver)
                    if bal is not None and bal != last_persisted:
                        try:
                            models['platform_account'].update_balance(platform, email, bal)
                            last_persisted = bal
                            state.add_log(f"{email} 检测到余额页，余额已更新: ${bal:.2f}")
                        except Exception as _e:
                            state.add_log(f"{email} 余额落库失败: {str(_e)[:80]}")
                    time.sleep(2)
                except Exception:
                    break
        except Exception as e:
            state.add_log(f"{email} 打开浏览器异常: {e}")
        finally:
            if driver:
                try:
                    close_driver(driver)
                except Exception:
                    pass
            state.open_browsers.discard(email)
            state.add_log(f"{email} 浏览器已关闭")

    threading.Thread(target=_do_open, daemon=True).start()
    return jsonify({"status": "started", "email": email})


@api.route('/api/accounts/open-browsers')
def get_open_browsers():
    """获取当前打开的浏览器会话列表。

    open_browsers 是**共享**资源（Chrome profile 目录按 email，跨平台唯一），
    取哪个 ctx 拿到的都是同一个集合——这里用默认 ctx 不是漏改。
    """
    state = get_app_state()
    return jsonify({"emails": list(state.open_browsers)})


@api.route('/api/recharge-logs')
def get_recharge_logs():
    models = get_models()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    email = request.args.get('email', '')
    status = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    logs, total = models['recharge_log'].get_paginated(
        page=page, page_size=page_size,
        email=email, status=status,
        date_from=date_from, date_to=date_to,
    )
    return jsonify({"data": logs, "total": total, "page": page, "page_size": page_size})


@api.route('/api/card-recharge-logs')
def get_recharge_logs_by_card():
    """某张卡的充值记录明细，供卡池/有效卡列表点开查看。

    路径特意不挂在 /api/recharge-logs/ 下——那里的 <email> 动态段会吞掉同级路径。
    """
    models = get_models()
    card_number = request.args.get('card_number', '')
    if not card_number:
        return jsonify({"error": "缺少 card_number"}), 400
    logs = models['recharge_log'].get_by_card(card_number)
    success = sum(1 for l in logs if l.get('status') == 'success')
    return jsonify({"data": logs, "total": len(logs), "success_total": success,
                    "card_number": card_number})


@api.route('/api/recharge-logs/<email>')
def get_recharge_logs_by_email(email):
    models = get_models()
    logs = models['recharge_log'].get_by_email(email)
    return jsonify(logs)


@api.route('/api/accounts/export', methods=['POST'])
def export_accounts():
    models = get_models()
    data = request.json or {}
    platform = _req_platform()

    mode = data.get('mode', 'filtered')
    selected_emails = data.get('emails', [])

    if mode == 'selected' and selected_emails:
        accounts = []
        for email in selected_emails:
            rows = models['account'].search(email)
            for r in rows:
                if r['email'] == email:
                    accounts.append(r)
                    break
    else:
        keyword = data.get('keyword', '')
        identity_status = data.get('identity_status', '')
        date_from = data.get('date_from', '')
        date_to = data.get('date_to', '')
        accounts, _ = models['account'].get_paginated(
            page=1, page_size=99999,
            keyword=keyword, identity_status=identity_status,
            date_from=date_from, date_to=date_to,
        )

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Accounts"

    # apikey 与余额是平台数据，导出的是当前选定平台那一份
    pa_map = models['platform_account'].map_by_email(
        platform, [a['email'] for a in accounts])

    headers = ["邮箱", "GitHub密码", "邮箱密码", "认证链接",
               f"apikey({platform})", f"余额({platform})"]
    header_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    header_font = Font(bold=True, size=11)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font

    row_idx = 2
    for acc in accounts:
        pa = pa_map.get(acc['email']) or {}
        ws.cell(row=row_idx, column=1, value=acc['email'])
        ws.cell(row=row_idx, column=2, value=acc.get('login_password') or '')
        ws.cell(row=row_idx, column=3, value=acc.get('email_password') or '')
        ws.cell(row=row_idx, column=4, value=acc.get('email_verify_link') or '')
        ws.cell(row=row_idx, column=5, value=pa.get('apikey') or '')
        ws.cell(row=row_idx, column=6, value=pa.get('credits_balance'))
        row_idx += 1

    for col in ws.columns:
        max_len = 0
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='accounts_export.xlsx',
    )


# ==================== 卡片分组 & 底料池 ====================

@api.route('/api/card-groups')
def get_card_groups():
    models = get_models()
    group_type = request.args.get('type', '')
    if group_type:
        groups = models['card_group'].get_by_type(group_type)
    else:
        groups = models['card_group'].get_all()
    return jsonify(groups)


@api.route('/api/card-groups', methods=['POST'])
def create_card_group():
    models = get_models()
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"error": "分组名称不能为空"}), 400
    # 已不区分分组类型，统一支付卡；type 仅为兼容保留，默认 payment
    group_type = data.get('type') or 'payment'
    if group_type not in ('bind', 'payment'):
        group_type = 'payment'
    description = data.get('description', '')
    group_id = models['card_group'].create(name, group_type, description)
    return jsonify({"id": group_id, "name": name, "type": group_type})


@api.route('/api/card-groups/<int:group_id>', methods=['PUT'])
def update_card_group(group_id):
    models = get_models()
    data = request.json or {}
    name = data.get('name')
    description = data.get('description')
    models['card_group'].update(group_id, name=name, description=description)
    return jsonify({"status": "ok"})


@api.route('/api/card-groups/<int:group_id>', methods=['DELETE'])
def delete_card_group(group_id):
    models = get_models()
    models['card_group'].delete(group_id)
    return jsonify({"status": "ok"})


def _attach_recharge_counts(card, counts):
    """给一张卡补充 recharge_total（累计成功充值次数）/ recharge_today（当日成功充值次数）。
    counts 由 RechargeLogModel.count_success_by_last4() 一次性聚合得到，按卡号末 4 位匹配。"""
    last4 = (card.get('card_number') or '').replace(' ', '')[-4:]
    c = counts.get(last4) or {}
    card['recharge_total'] = c.get('total', 0)
    card['recharge_today'] = c.get('today', 0)
    return card


@api.route('/api/card-pool/<int:group_id>')
def get_card_pool(group_id):
    models = get_models()
    platform = _req_platform()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    bucket = request.args.get('bucket', '')  # ''=全部 / valid / unverified / invalid

    # 先按当前日期刷新过期状态，列表里直接能看到哪些卡已过期
    models['card_pool'].refresh_expired_status(group_id)
    cards, total = models['card_pool'].get_by_group(
        platform, group_id, page=page, page_size=page_size, bucket=bucket)

    # 标记有效卡 + 选卡规则状态（供列表状态列展示 3DS临时/24h次数冷却）。
    # 全部按当前平台算——同一张卡在别的平台的冷却与绑定跟这个列表无关。
    recharge_counts = models['recharge_log'].count_success_by_last4(platform)
    for card in cards:
        num = card['card_number']
        card['is_valid'] = models['valid_card'].is_valid(platform, num)
        card['tds_cooldown'] = models['card_state'].in_tds_cooldown(platform, num)
        card['rate_cooldown'] = models['recharge_log'].success_count_since(platform, num, 24) >= 2
        card['bound_email'] = models['valid_card'].get_bound_email(platform, num)
        _attach_recharge_counts(card, recharge_counts)

    buckets = models['card_pool'].count_buckets(platform, group_id)
    return jsonify({"data": cards, "total": total, "page": page, "page_size": page_size,
                    "bucket": bucket, "buckets": buckets, "platform": platform})


@api.route('/api/card-pool/merge', methods=['POST'])
def merge_card_pools():
    """把多个源分组里的"非无效"卡（有效+未验证）移动合并到一个新分组。"""
    models = get_models()
    data = request.json or {}
    source_ids = data.get('source_group_ids') or []
    name = (data.get('name') or '').strip()
    # 已不区分分组类型，统一支付卡
    group_type = data.get('type') or 'payment'
    if group_type not in ('bind', 'payment'):
        group_type = 'payment'
    # 转移范围：non_invalid（有效+未验证，默认）/ valid（仅有效卡）
    bucket = data.get('bucket') or 'non_invalid'
    if bucket not in ('non_invalid', 'valid'):
        return jsonify({"error": "转移范围无效"}), 400
    if not source_ids:
        return jsonify({"error": "未选择源分组"}), 400
    if not name:
        return jsonify({"error": "未填写新分组名称"}), 400
    # 源分组存在性校验
    for gid in source_ids:
        if not models['card_group'].get_by_id(gid):
            return jsonify({"error": f"源分组 {gid} 不存在"}), 404

    new_id = models['card_group'].create(name, group_type=group_type)
    result = models['card_pool'].move_non_invalid_to_group(
        _req_platform(), source_ids, new_id, bucket=bucket)
    return jsonify({"status": "ok", "group_id": new_id,
                    "moved": result['moved'], "deduped": result['deduped']})


@api.route('/api/card-pool/<int:group_id>/move', methods=['POST'])
def move_cards_to_group(group_id):
    """把源分组内指定桶的卡片，按数量上限移动到已存在的目标分组。

    与 /merge 的区别：目标分组必须已存在，可限制数量与桶，重复卡跳过而非删除。
    """
    models = get_models()
    data = request.json or {}

    target_id = data.get('target_group_id')
    if not isinstance(target_id, int) or isinstance(target_id, bool):
        return jsonify({"error": "未选择目标分组"}), 400
    if target_id == group_id:
        return jsonify({"error": "源分组与目标分组相同"}), 400

    bucket = data.get('bucket')
    if bucket not in CardPoolModel.MOVABLE_BUCKETS:
        return jsonify({"error": "卡片范围无效"}), 400

    limit = data.get('limit')
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return jsonify({"error": "移动数量必须为正整数"}), 400

    if not models['card_group'].get_by_id(group_id):
        return jsonify({"error": "源分组不存在"}), 404
    if not models['card_group'].get_by_id(target_id):
        return jsonify({"error": "目标分组不存在"}), 404

    result = models['card_pool'].move_bucket_to_group(
        _req_platform(), group_id, target_id, bucket, limit)
    return jsonify({"status": "ok", "moved": result['moved'], "skipped": result['skipped']})


@api.route('/api/card-pool/<int:group_id>/delete-invalid', methods=['POST'])
def delete_invalid_cards(group_id):
    """删除某分组内所有无效卡（invalid + expired）。"""
    models = get_models()
    if not models['card_group'].get_by_id(group_id):
        return jsonify({"error": "分组不存在"}), 404
    deleted = models['card_pool'].delete_invalid_by_group(_req_platform(), group_id)
    return jsonify({"status": "ok", "deleted": deleted})


@api.route('/api/card-pool/<int:group_id>/upload', methods=['POST'])
def upload_card_pool(group_id):
    models = get_models()

    group = models['card_group'].get_by_id(group_id)
    if not group:
        return jsonify({"error": "分组不存在"}), 404

    if 'file' not in request.files:
        return jsonify({"error": "未上传文件"}), 400

    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({"error": "仅支持 .xlsx/.xls 文件"}), 400

    upload_dir = str(get_data_dir() / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, f"pool_upload_{group_id}.xlsx")
    file.save(save_path)

    cards, errors = card_service.parse_excel(save_path)
    if not cards:
        return jsonify({"error": "解析失败", "details": errors}), 400

    added, skipped, conflicts = models['card_pool'].add_cards(group_id, cards)

    # 上传时即按有效期判定，过期卡会入库但标记为 expired（不参与任务），这里回传数量供前端提示
    expired = sum(1 for c in cards if is_card_expired(c.get('expiry_month'), c.get('expiry_year')))

    return jsonify({
        "added": added,
        "skipped": skipped,
        "conflicts": conflicts,
        "expired": expired,
        "total_parsed": len(cards),
        "errors": errors,
    })


@api.route('/api/card-pool/card/<int:card_id>', methods=['DELETE'])
def delete_pool_card(card_id):
    models = get_models()
    models['card_pool'].delete_card(card_id)
    return jsonify({"status": "ok"})


@api.route('/api/card-pool/<int:group_id>/clear', methods=['POST'])
def clear_card_pool(group_id):
    models = get_models()
    deleted = models['card_pool'].delete_by_group(group_id)
    return jsonify({"deleted": deleted})


# ==================== 有效卡 ====================

_POOL_STATUS_ZH = {'': '在库(未验证)', 'paid': '有效(已支付)', 'invalid': '无效',
                   'expired': '已过期', 'bound': '已绑定'}


def _valid_card_status(models, card, platform):
    """给一张有效卡补充选卡状态：绑定账号、3DS临时冷却、24h次数冷却、汇总状态文案、池内分组/状态。

    全部按 platform 算：冷却、池内状态在各平台各不相同。"""
    num = card.get('card_number', '')
    tds = models['card_state'].in_tds_cooldown(platform, num)
    rate = models['recharge_log'].success_count_since(platform, num, 24) >= 2
    card['bound_email'] = card.get('source_email', '') if card.get('source_type') == 'payment' else ''
    card['tds_cooldown'] = bool(tds)
    card['rate_cooldown'] = bool(rate)
    card['tds_until'] = models['card_state'].get_tds_until(platform, num)
    card['status_text'] = '3DS临时冷却' if tds else ('24h达2次冷却' if rate else '可用')
    # 池内位置：该有效卡当前在卡池哪个分组、什么状态（解释"为何不计入某分组的有效桶"）
    locs = models['card_pool'].get_locations_by_number(platform, num)
    if locs:
        card['pool_group'] = '，'.join((l.get('group_name') or str(l.get('group_id'))) for l in locs)
        card['pool_status'] = '，'.join(_POOL_STATUS_ZH.get(l.get('status') or '', l.get('status') or '') for l in locs)
    else:
        card['pool_group'] = ''
        card['pool_status'] = '不在卡池'
    return card


@api.route('/api/valid-cards')
def get_valid_cards():
    models = get_models()
    platform = _req_platform()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    source_type = request.args.get('source_type', '')
    keyword = request.args.get('keyword', '')
    cards, total = models['valid_card'].get_all(
        platform, page=page, page_size=page_size,
        source_type=source_type, keyword=keyword,
    )
    recharge_counts = models['recharge_log'].count_success_by_last4(platform)
    for c in cards:
        _valid_card_status(models, c, platform)
        _attach_recharge_counts(c, recharge_counts)
    summary = models['valid_card'].get_summary(platform)
    return jsonify({"data": cards, "total": total, "page": page, "page_size": page_size,
                    "summary": summary, "platform": platform})


@api.route('/api/valid-cards/export')
def export_valid_cards():
    """导出全部有效卡为 xlsx：中文表头 + 关联账号信息 + 完整信用卡信息（不脱敏）。"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    from datetime import datetime
    models = get_models()
    platform = _req_platform()
    source_type = request.args.get('source_type', '')
    cards = models['valid_card'].get_all_for_export(platform, source_type)

    # 关联账号信息：身份层取密码，平台层取该平台的状态
    acct_map = {a['email']: a for a in models['account'].get_all(order_desc=False)}
    pa_map = models['platform_account'].map_by_email(platform)

    headers = ['卡号', '有效期(月)', '有效期(年)', '安全码CVC', '名', '姓',
               '国家', '地址', '地址2', '城市', '州', '邮编', '公司',
               '来源', '关联账号', 'GitHub密码', '邮箱密码', '身份状态',
               f'平台状态({platform})',
               '卡状态', '累计充值次数', '当日充值次数', '验证时间']
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '有效卡'
    ws.append(headers)
    recharge_counts = models['recharge_log'].count_success_by_last4(platform)
    for c in cards:
        _valid_card_status(models, c, platform)
        _attach_recharge_counts(c, recharge_counts)
        email = c.get('source_email', '') or ''
        acct = acct_map.get(email, {})
        ws.append([
            c.get('card_number', ''), c.get('expiry_month', ''), c.get('expiry_year', ''),
            c.get('cvc', ''), c.get('first_name', ''), c.get('last_name', ''),
            c.get('country', ''), c.get('address', ''), c.get('address2', ''),
            c.get('city', ''), c.get('state', ''), c.get('zip', ''), c.get('company', ''),
            ('绑定' if c.get('source_type') == 'bind' else '支付'),
            email, acct.get('login_password', ''), acct.get('email_password', ''),
            acct.get('identity_status', ''),
            (pa_map.get(email) or {}).get('status', ''),
            c.get('status_text', ''),
            c.get('recharge_total', 0), c.get('recharge_today', 0), c.get('validated_at', ''),
        ])

    # 列宽自适应（按每列最长内容估算，含表头）
    for col_idx in range(1, len(headers) + 1):
        letter = get_column_letter(col_idx)
        maxlen = 0
        for cell in ws[letter]:
            v = '' if cell.value is None else str(cell.value)
            # 中文按 2 宽度估算
            w = sum(2 if ord(ch) > 127 else 1 for ch in v)
            maxlen = max(maxlen, w)
        ws.column_dimensions[letter].width = min(max(maxlen + 2, 8), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"有效卡_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@api.route('/api/daily/start', methods=['POST'])
def start_daily_pipeline():
    """启动每日充值任务：选定卡池分组，逐账号轮转充值直到卡池消耗完。"""
    models = get_models()
    data = request.json or {}

    group_id = data.get('group_id')
    if not group_id:
        return jsonify({"error": "未指定卡池分组"}), 400
    group = models['card_group'].get_by_id(group_id)
    if not group:
        return jsonify({"error": "卡池分组不存在"}), 404

    platform = _req_platform(required=True)
    if not platform:
        return jsonify({"error": "未指定平台"}), 400

    # 闸门必须**在拿到 platform 之后**判，而且只判这个平台。
    # 曾经是先取全局单例再判 is_running，于是一个平台在跑就挡住所有平台——
    # 这正是 AC1 要解开的那道锁。
    state = get_ctx(platform)
    if state.is_running:
        return jsonify({"error": f"{platform} 已有任务在运行"}), 400

    login_password = data.get('login_password') or None
    captcha_api_key = data.get('captcha_api_key')
    # 充值默认用 Multibot 解支付页 hCaptcha；可传 captcha_server='2captcha.com' 切回
    captcha_server = data.get('captcha_server') or 'api.multibot.cloud'

    # 启动门：分组要有可选卡（排除无效/过期/冷却），且要有可充值账号。
    # 账号判据必须与 run_daily_pipeline._payable_now 用同一组常量——此前这里只排除
    # ('banned','archived') 而流水线排除四项，于是「启动时说有 N 个可充值账号，
    # 实际跑起来只有 M 个」。
    # platform 必须显式传下去：_eligible_cards 在 platform=None 时回落 AppState.platform，
    # 也就是**上一次运行**的那个平台。单平台下「碰巧对」，于是这个启动门一直在用
    # 另一个平台的卡数做判断。exclude_used=False 同样是必须的——计数调用点不能扣掉
    # 「本次运行已用」，否则报给用户的可选卡数会偏小。
    eligible = len(state._eligible_cards(group_id, exclude_used=False, platform=platform))
    if not eligible:
        return jsonify({"error": "该分组无可选卡（全部无效/过期或冷却中），无事可做"}), 400

    accts = models['account'].get_all(order_desc=False)
    pa_map = models['platform_account'].map_by_email(platform)
    account_count = sum(
        1 for a in accts
        if (login_password or a.get('login_password'))
        and not is_identity_terminal(a.get('identity_status'))
        and not is_platform_terminal((pa_map.get(a['email']) or {}).get('status'))
    )
    if account_count == 0:
        return jsonify({"error": "无可充值账号（需有登录密码、身份与平台状态均非终态），无事可做"}), 400

    import threading
    threading.Thread(
        target=state.run_daily_pipeline,
        args=(platform, group_id, login_password, captcha_api_key, captcha_server),
        daemon=True,
    ).start()

    return jsonify({"status": "started", "usable_cards": eligible,
                    "accounts": account_count, "group_name": group['name']})


@api.route('/api/daily/subscribe/start', methods=['POST'])
def start_daily_subscribe_pipeline():
    """启动每日订阅任务：账号轮转——未注册先注册、已注册登录订阅，成功即换下一个账号，
    直到无可选卡 / 无待订阅账号 / 用户停止。与每日充值任务互斥（共用 is_running 闸门）。"""
    models = get_models()
    data = request.json or {}

    group_id = data.get('group_id')
    if not group_id:
        return jsonify({"error": "未指定卡池分组"}), 400
    group = models['card_group'].get_by_id(group_id)
    if not group:
        return jsonify({"error": "卡池分组不存在"}), 404

    platform = _req_platform(required=True)
    if not platform:
        return jsonify({"error": "未指定平台"}), 400

    # 闸门在拿到 platform 之后判，且只判这个平台（理由同每日充值端点）。
    state = get_ctx(platform)
    if state.is_running:
        return jsonify({"error": f"{platform} 已有任务在运行"}), 400

    captcha_api_key = data.get('captcha_api_key')
    # 订阅默认用 Multibot 解 hCaptcha；可传 captcha_server='2captcha.com' 切回
    captcha_server = data.get('captcha_server') or 'api.multibot.cloud'

    # 启动门：分组要有可选卡，且要有待订阅账号（判据同 _needing）
    # platform / exclude_used 必须显式传，理由同每日充值那个启动门。
    eligible = len(state._eligible_cards(group_id, exclude_used=False, platform=platform))
    if not eligible:
        return jsonify({"error": "该分组无可选卡（全部无效/过期或冷却中），无事可做"}), 400

    accts = models['account'].get_all(order_desc=False)
    pa_map = models['platform_account'].map_by_email(platform)
    account_count = sum(
        1 for a in accts
        if not is_identity_terminal(a.get('identity_status'))
        and not is_platform_terminal((pa_map.get(a['email']) or {}).get('status'))
    )
    if account_count == 0:
        return jsonify({"error": "无待订阅账号（身份或平台状态均已终态），无事可做"}), 400

    import threading
    threading.Thread(
        target=state.run_daily_subscribe_pipeline,
        args=(platform, group_id, captcha_api_key, captcha_server),
        daemon=True,
    ).start()

    return jsonify({"status": "started", "usable_cards": eligible,
                    "accounts": account_count, "group_name": group['name']})


# ==================== 代理 IP 池 ====================

def _mask_proxy(row):
    """列表展示用:凭据打码,只留首尾几位。"""
    def _m(s):
        s = s or ''
        return s if len(s) <= 4 else f"{s[:3]}***{s[-2:]}"
    return {
        'id': row['id'], 'host': row['host'], 'port': row['port'],
        'username': _m(row.get('username')), 'password': _m(row.get('password')),
        'status': row.get('status') or '', 'created_at': row.get('created_at'),
    }


@api.route('/api/proxies')
def get_proxies():
    models = get_models()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 50))
    rows, total = models['proxy'].get_all(page=page, page_size=page_size)
    return jsonify({"data": [_mask_proxy(r) for r in rows], "total": total,
                    "page": page, "page_size": page_size})


@api.route('/api/proxies/import', methods=['POST'])
def import_proxies():
    """粘贴导入:body {text} 每行一个 user:pass@host:port(兼容纯冒号)。"""
    models = get_models()
    text = (request.json or {}).get('text', '')
    if not text.strip():
        return jsonify({"error": "内容为空"}), 400
    added, skipped = models['proxy'].add_proxies(text)
    return jsonify({"added": added, "skipped": skipped, "total": models['proxy'].count()})


@api.route('/api/proxies/<int:proxy_id>', methods=['DELETE'])
def delete_proxy(proxy_id):
    models = get_models()
    models['proxy'].delete(proxy_id)
    return jsonify({"status": "ok"})


@api.route('/api/proxies/clear', methods=['POST'])
def clear_proxies():
    models = get_models()
    models['proxy'].clear()
    return jsonify({"status": "ok"})
