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
from src.utils import is_card_expired

api = Blueprint('api', __name__)


def get_app_state():
    """获取全局 AppState（在 app.py 中注入）"""
    from flask import current_app
    return current_app.config['APP_STATE']


def get_db():
    from flask import current_app
    return current_app.config['DB']


def get_models():
    from flask import current_app
    return current_app.config['MODELS']


@api.route('/api/status')
def get_status():
    """全局状态 + 各 worker 概览。

    向后兼容：顶层字段全部保留原语义，workers 数组是新增的旁挂字段。
    串行运行时 workers 只有 W1，顶层 current_action 即 W1 的动作。
    """
    state = get_app_state()
    models = get_models()

    total_inventory = models['account'].count()

    return jsonify({
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
    })


@api.route('/api/workers/<worker_id>/logs')
def get_worker_logs(worker_id):
    """按 worker 增量拉取日志。

    index 传上次返回的 next_index；worker 不存在时回落到主 worker，
    保证前端在 worker 数变化时不会拿到 404。
    """
    state = get_app_state()
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
    state = get_app_state()
    if state.is_running:
        return jsonify({"error": "Task already running"}), 400

    data = request.json or {}
    count = data.get('count', 1)
    card_info_list = data.get('card_info_list', None)
    cf_password = data.get('cf_password', None)
    max_bindable_cards = data.get('max_bindable_cards', 2)
    captcha_api_key = data.get('captcha_api_key', None)

    import threading
    threading.Thread(
        target=state.run_batch_task,
        args=(count, card_info_list, cf_password, max_bindable_cards, captcha_api_key),
        daemon=True,
    ).start()

    return jsonify({"status": "started"})


@api.route('/api/stop', methods=['POST'])
def stop_task():
    state = get_app_state()
    if not state.is_running:
        return jsonify({"error": "No running task"}), 400

    state.force_stop()
    return jsonify({"status": "stopping"})


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
    state = get_app_state()
    models = get_models()
    active_task_id = state.current_card_task_id if state.is_running else None
    deleted = models['card_binding'].cleanup_stale_pending(active_task_id)
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

    headers = ["序号", "卡号(后4位)", "状态", "绑定账号", "错误信息", "处理时间", "任务批次",
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
        ws.cell(row=row_idx, column=2, value=f"****{r['card_display']}")
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
    status = request.args.get('status', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')

    accounts, total = models['account'].get_paginated(
        page=page, page_size=page_size,
        keyword=keyword, status=status,
        date_from=date_from, date_to=date_to,
    )

    emails = [acc['email'] for acc in accounts]
    card_counts = models['card_binding'].count_by_emails(emails)

    data = []
    for acc in accounts:
        data.append({
            "email": acc['email'],
            "password": acc.get('cf_password') or '',
            "status": acc.get('status') or '',
            "time": acc.get('created_at') or '',
            "email_password": acc.get('email_password') or '',
            "card_count": card_counts.get(acc['email'], 0),
            "credits_balance": acc.get('credits_balance'),
            "balance_updated_at": acc.get('balance_updated_at') or '',
        })

    return jsonify({"data": data, "total": total, "page": page, "page_size": page_size})


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

    count = models['account'].delete_by_emails(emails)
    return jsonify({"deleted": count})


@api.route('/api/accounts/<email>/cards')
def get_account_cards(email):
    models = get_models()
    cards = models['card_binding'].get_by_email(email)
    return jsonify(cards)


@api.route('/api/accounts/recharge', methods=['POST'])
def recharge_account():
    state = get_app_state()
    models = get_models()

    if state.is_running:
        return jsonify({"error": "有任务正在运行，请等待完成后再操作"}), 400

    data = request.json or {}
    email = data.get('email', '')
    payment_group_id = data.get('payment_group_id')
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

    cf_password = account.get('cf_password')
    if not cf_password:
        return jsonify({"error": "该账号没有保存 CF 密码"}), 400

    status = account.get('status', '')
    if 'bound' not in status:
        return jsonify({"error": "该账号未绑定信用卡，无法充值"}), 400

    # 标记为运行中，在后台线程执行充值
    state.is_running = True
    state.current_action = f"正在为 {email} 充值..."
    state._patch_prints()

    import threading

    def _do_recharge():
        try:
            state._recharge_one_account(email, cf_password, payment_group_id)
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
    """打开浏览器查看账号的 Cloudflare 控制台，按账号独立，不阻塞全局任务"""
    state = get_app_state()
    models = get_models()

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

    cf_password = account.get('cf_password')
    if not cf_password:
        return jsonify({"error": "该账号没有保存 CF 密码"}), 400

    # 预约 profile：与任务 worker 的账号占用共用一把锁，避免同一 Chrome profile
    # 被 worker 与手动会话同时使用（会互删 Singleton 锁导致浏览器崩溃）
    ok, reason = state.account_registry.try_open_manual(email)
    if not ok:
        return jsonify({"error": reason}), 409

    state.add_log(f"{email} 正在打开浏览器...")

    import threading

    def _do_open():
        from src.browser.driver import create_driver, login_cloudflare, close_driver
        driver = None
        try:
            driver = create_driver(headless=False, profile_id=email)
            account_id = login_cloudflare(
                driver, email, cf_password, account.get('email_password'))
            if account_id:
                state.add_log(f"{email} 浏览器已打开")
            elif getattr(driver, 'account_banned', False):
                models['account'].update_status(email, 'banned')
                state.add_log(f"{email} 账号已被 Cloudflare 封禁，已在数据库标记为 banned")
            else:
                state.add_log(f"{email} 打开浏览器登录失败")

            # 等待用户手动关闭浏览器；期间被动监听 credit-balance 接口：
            # 用户手动进入 AI Gateway credits 页时，页面会自请求该接口，响应经
            # page.on("response") 捕获进 driver.credit_balance；这里轮询该值，
            # 一旦变化即落库刷新余额（driver.title 调用会驱动 Playwright 事件派发）。
            import time
            last_persisted = None
            while True:
                try:
                    _ = driver.title
                    bal = getattr(driver, 'credit_balance', None)
                    if bal is not None and bal != last_persisted:
                        try:
                            models['account'].update_balance(email, bal)
                            last_persisted = bal
                            state.add_log(f"{email} 检测到 credits 页，余额已更新: ${bal:.2f}")
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
    """获取当前打开的浏览器会话列表"""
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


@api.route('/api/recharge-logs/<email>')
def get_recharge_logs_by_email(email):
    models = get_models()
    logs = models['recharge_log'].get_by_email(email)
    return jsonify(logs)


@api.route('/api/accounts/export', methods=['POST'])
def export_accounts():
    models = get_models()
    data = request.json or {}

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
        status = data.get('status', '')
        date_from = data.get('date_from', '')
        date_to = data.get('date_to', '')
        accounts, _ = models['account'].get_paginated(
            page=1, page_size=99999,
            keyword=keyword, status=status,
            date_from=date_from, date_to=date_to,
        )

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Accounts"

    headers = ["邮箱", "CF密码", "邮箱密码", "状态", "注册时间",
               "卡号", "有效期", "CVC", "持卡人",
               "国家", "地址1", "地址2", "城市", "州/省", "邮编", "公司",
               "绑卡状态", "绑卡时间", "Credits余额", "余额更新时间"]
    header_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    header_font = Font(bold=True, size=11)
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font

    row_idx = 2
    for acc in accounts:
        email = acc['email']
        cards = models['card_binding'].get_by_email(email)

        def write_acc_cols(r):
            ws.cell(row=r, column=1, value=email)
            ws.cell(row=r, column=2, value=acc.get('cf_password') or '')
            ws.cell(row=r, column=3, value=acc.get('email_password') or '')
            ws.cell(row=r, column=4, value=acc.get('status') or '')
            ws.cell(row=r, column=5, value=acc.get('created_at') or '')
            ws.cell(row=r, column=19, value=acc.get('credits_balance'))
            ws.cell(row=r, column=20, value=acc.get('balance_updated_at') or '')

        if cards:
            for card in cards:
                write_acc_cols(row_idx)
                ws.cell(row=row_idx, column=6, value=card.get('card_number', ''))
                expiry = f"{card.get('expiry_month', '')}/{card.get('expiry_year', '')}" if card.get('expiry_month') else ''
                ws.cell(row=row_idx, column=7, value=expiry)
                ws.cell(row=row_idx, column=8, value=card.get('cvc', ''))
                ws.cell(row=row_idx, column=9, value=card.get('card_holder', ''))
                ws.cell(row=row_idx, column=10, value=card.get('country', ''))
                ws.cell(row=row_idx, column=11, value=card.get('address', ''))
                ws.cell(row=row_idx, column=12, value=card.get('address2', ''))
                ws.cell(row=row_idx, column=13, value=card.get('city', ''))
                ws.cell(row=row_idx, column=14, value=card.get('state', ''))
                ws.cell(row=row_idx, column=15, value=card.get('zip', ''))
                ws.cell(row=row_idx, column=16, value=card.get('company', ''))
                ws.cell(row=row_idx, column=17, value=card.get('status', ''))
                ws.cell(row=row_idx, column=18, value=card.get('attempted_at') or '')
                row_idx += 1
        else:
            write_acc_cols(row_idx)
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
    group_type = data.get('type', 'bind')
    if group_type not in ('bind', 'payment'):
        return jsonify({"error": "分组类型必须是 bind 或 payment"}), 400
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


@api.route('/api/card-pool/<int:group_id>')
def get_card_pool(group_id):
    models = get_models()
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    bucket = request.args.get('bucket', '')  # ''=全部 / valid / unverified / invalid

    # 先按当前日期刷新过期状态，列表里直接能看到哪些卡已过期
    models['card_pool'].refresh_expired_status(group_id)
    cards, total = models['card_pool'].get_by_group(
        group_id, page=page, page_size=page_size, bucket=bucket)

    # 标记有效卡 + 选卡规则状态（供列表状态列展示 3DS临时/24h次数冷却）
    for card in cards:
        num = card['card_number']
        card['is_valid'] = models['valid_card'].is_valid(num)
        card['tds_cooldown'] = models['card_state'].in_tds_cooldown(num)
        card['rate_cooldown'] = models['recharge_log'].success_count_since(num, 24) >= 2
        card['bound_email'] = models['valid_card'].get_bound_email(num)

    buckets = models['card_pool'].count_buckets(group_id)
    return jsonify({"data": cards, "total": total, "page": page, "page_size": page_size,
                    "bucket": bucket, "buckets": buckets})


@api.route('/api/card-pool/merge', methods=['POST'])
def merge_card_pools():
    """把多个源分组里的"非无效"卡（有效+未验证）移动合并到一个新分组。"""
    models = get_models()
    data = request.json or {}
    source_ids = data.get('source_group_ids') or []
    name = (data.get('name') or '').strip()
    group_type = data.get('type') or 'bind'
    if not source_ids:
        return jsonify({"error": "未选择源分组"}), 400
    if not name:
        return jsonify({"error": "未填写新分组名称"}), 400
    if group_type not in ('bind', 'payment'):
        return jsonify({"error": "分组类型无效"}), 400
    # 源分组存在性校验
    for gid in source_ids:
        if not models['card_group'].get_by_id(gid):
            return jsonify({"error": f"源分组 {gid} 不存在"}), 404

    new_id = models['card_group'].create(name, group_type=group_type)
    result = models['card_pool'].move_non_invalid_to_group(source_ids, new_id)
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

    result = models['card_pool'].move_bucket_to_group(group_id, target_id, bucket, limit)
    return jsonify({"status": "ok", "moved": result['moved'], "skipped": result['skipped']})


@api.route('/api/card-pool/<int:group_id>/delete-invalid', methods=['POST'])
def delete_invalid_cards(group_id):
    """删除某分组内所有无效卡（invalid + expired）。"""
    models = get_models()
    if not models['card_group'].get_by_id(group_id):
        return jsonify({"error": "分组不存在"}), 404
    deleted = models['card_pool'].delete_invalid_by_group(group_id)
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

_POOL_STATUS_ZH = {'': '在库(未验证)', 'paid': '有效(已支付)', 'invalid': '无效', 'expired': '已过期'}


def _valid_card_status(models, card):
    """给一张有效卡补充选卡状态：绑定账号、3DS临时冷却、24h次数冷却、汇总状态文案、池内分组/状态。"""
    num = card.get('card_number', '')
    tds = models['card_state'].in_tds_cooldown(num)
    rate = models['recharge_log'].success_count_since(num, 24) >= 2
    card['bound_email'] = card.get('source_email', '') if card.get('source_type') == 'payment' else ''
    card['tds_cooldown'] = bool(tds)
    card['rate_cooldown'] = bool(rate)
    card['tds_until'] = models['card_state'].get_tds_until(num)
    card['status_text'] = '3DS临时冷却' if tds else ('24h达2次冷却' if rate else '可用')
    # 池内位置：该有效卡当前在卡池哪个分组、什么状态（解释"为何不计入某分组的有效桶"）
    locs = models['card_pool'].get_locations_by_number(num)
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
    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    source_type = request.args.get('source_type', '')
    keyword = request.args.get('keyword', '')
    cards, total = models['valid_card'].get_all(
        page=page, page_size=page_size,
        source_type=source_type, keyword=keyword,
    )
    for c in cards:
        _valid_card_status(models, c)
    summary = models['valid_card'].get_summary()
    return jsonify({"data": cards, "total": total, "page": page, "page_size": page_size, "summary": summary})


@api.route('/api/valid-cards/export')
def export_valid_cards():
    """导出全部有效卡为 xlsx：中文表头 + 关联 Cloudflare 账号信息 + 完整信用卡信息（不脱敏）。"""
    import openpyxl
    from openpyxl.utils import get_column_letter
    from datetime import datetime
    models = get_models()
    source_type = request.args.get('source_type', '')
    cards = models['valid_card'].get_all_for_export(source_type)

    # 关联账号信息：source_email -> {cf_password, email_password, status}
    acct_map = {a['email']: a for a in models['account'].get_all(order_desc=False)}

    headers = ['卡号', '有效期(月)', '有效期(年)', '安全码CVC', '名', '姓',
               '国家', '地址', '地址2', '城市', '州', '邮编', '公司',
               '来源', '关联CF账号', 'CF登录密码', '邮箱密码', '账号状态',
               '卡状态', '验证时间']
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '有效卡'
    ws.append(headers)
    for c in cards:
        _valid_card_status(models, c)
        email = c.get('source_email', '') or ''
        acct = acct_map.get(email, {})
        ws.append([
            c.get('card_number', ''), c.get('expiry_month', ''), c.get('expiry_year', ''),
            c.get('cvc', ''), c.get('first_name', ''), c.get('last_name', ''),
            c.get('country', ''), c.get('address', ''), c.get('address2', ''),
            c.get('city', ''), c.get('state', ''), c.get('zip', ''), c.get('company', ''),
            ('绑定' if c.get('source_type') == 'bind' else '支付'),
            email, acct.get('cf_password', ''), acct.get('email_password', ''),
            acct.get('status', ''), c.get('status_text', ''), c.get('validated_at', ''),
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
    """启动每日一键流水线：补绑已有账号 → 注册新号 → 批量充值"""
    state = get_app_state()
    if state.is_running:
        return jsonify({"error": "有任务正在运行"}), 400

    models = get_models()
    data = request.json or {}
    bind_group_id = data.get('bind_group_id')
    if not bind_group_id:
        return jsonify({"error": "未指定绑卡分组"}), 400

    group = models['card_group'].get_by_id(bind_group_id)
    if not group:
        return jsonify({"error": "绑卡分组不存在"}), 404

    cf_password = data.get('cf_password')
    payment_group_id = data.get('payment_group_id') or None
    max_bindable_cards = data.get('max_bindable_cards', 2)
    captcha_api_key = data.get('captcha_api_key')

    # 校验：绑卡分组有可用卡 或 有已绑卡账号可充值，二者至少其一，否则无事可做。
    # 注意：这里不再用 has_today_record 排除今日已充账号——流水线阶段2 会放行所有绑卡账号
    # （今日已充的转去执行账单支付/复查），启动门若继续排除会与流水线不一致，导致「没有绑卡
    # 数据、只想跑充值」的场景被误拦。只要有账号绑卡数≥1 即允许启动，无卡时自动跳过补绑/注册。
    cards, unusable = models['card_pool'].get_usable_cards_as_list(bind_group_id)
    has_rechargeable = False
    if not cards:
        accts = models['account'].get_all(order_desc=False)
        emails = [a['email'] for a in accts]
        counts = models['card_binding'].count_by_emails(emails)
        has_rechargeable = any(
            a.get('cf_password') and counts.get(a['email'], 0) >= 1
            for a in accts
        )
        if not has_rechargeable:
            if unusable:
                return jsonify({"error": f"绑卡分组 {len(unusable)} 张卡均已无效，且无已绑卡账号可充值，无事可做"}), 400
            return jsonify({"error": "绑卡分组无可用卡，且无已绑卡账号可充值，无事可做"}), 400

    import threading
    threading.Thread(
        target=state.run_daily_pipeline,
        args=(bind_group_id, payment_group_id, cf_password,
              max_bindable_cards, captcha_api_key),
        daemon=True,
    ).start()

    return jsonify({"status": "started", "usable_cards": len(cards),
                    "group_name": group['name']})
