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

    state.stop_requested = True
    return jsonify({"status": "stopping"})


@api.route('/api/card/template')
def download_card_template():
    template_path = card_service.generate_template()
    directory = os.path.dirname(template_path)
    filename = os.path.basename(template_path)
    return send_from_directory(directory, filename, as_attachment=True)


@api.route('/api/card/upload', methods=['POST'])
def upload_card_excel():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({"error": "Only .xlsx/.xls files are supported"}), 400

    upload_dir = str(get_data_dir() / "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, "uploaded_cards.xlsx")
    file.save(save_path)

    cards, errors = card_service.parse_excel(save_path)

    if errors and not cards:
        return jsonify({"error": "Parse failed", "details": errors}), 400

    return jsonify({
        "total": len(cards),
        "errors": errors,
        "preview": [
            {"index": i, "card_display": f"****{c['number'][-4:]}", "name": f"{c['first_name']} {c['last_name']}"}
            for i, c in enumerate(cards)
        ]
    })


@api.route('/api/card/check-unfinished')
def check_unfinished_cards():
    """检测是否有上次未完成的卡"""
    models = get_models()
    upload_path = str(get_data_dir() / "uploads" / "uploaded_cards.xlsx")
    if not os.path.exists(upload_path):
        return jsonify({"has_unfinished": False, "remaining": 0, "total": 0})

    cards, _ = card_service.parse_excel(upload_path)
    if not cards:
        return jsonify({"has_unfinished": False, "remaining": 0, "total": 0})

    already_bound = models['card_binding'].get_successfully_bound_card_numbers()
    remaining = [c for c in cards if c.get('number') not in already_bound]

    has_unfinished = 0 < len(remaining) < len(cards)
    return jsonify({
        "has_unfinished": has_unfinished,
        "remaining": len(remaining),
        "total": len(cards),
        "bound": len(cards) - len(remaining),
    })


@api.route('/api/card/start', methods=['POST'])
def start_card_driven_task():
    state = get_app_state()
    if state.is_running:
        return jsonify({"error": "Task already running"}), 400

    data = request.json or {}
    cf_password = data.get('cf_password', None)
    max_bindable_cards = data.get('max_bindable_cards', 2)
    captcha_api_key = data.get('captcha_api_key', None)

    upload_path = str(get_data_dir() / "uploads" / "uploaded_cards.xlsx")
    if not os.path.exists(upload_path):
        return jsonify({"error": "Please upload credit card Excel file first"}), 400

    cards, errors = card_service.parse_excel(upload_path)
    if not cards:
        return jsonify({"error": "No valid data in Excel", "details": errors}), 400

    import threading
    threading.Thread(
        target=state.run_card_driven_task,
        args=(cards, cf_password, max_bindable_cards, captcha_api_key),
        daemon=True,
    ).start()

    return jsonify({"status": "started", "total_cards": len(cards)})


@api.route('/api/card/status')
def get_card_status():
    state = get_app_state()
    models = get_models()

    empty_resp = {"data": [], "total": 0, "page": 1, "page_size": 20,
                  "summary": {"total": 0, "success": 0, "failed": 0, "pending": 0}}

    if not state.current_card_task_id:
        return jsonify(empty_resp)

    page = int(request.args.get('page', 1))
    page_size = int(request.args.get('page_size', 20))
    status_filter = request.args.get('status', '')
    keyword = request.args.get('keyword', '')

    records, total = models['card_binding'].get_paginated_by_task(
        state.current_card_task_id,
        page=page, page_size=page_size,
        status=status_filter, keyword=keyword,
    )
    summary = models['card_binding'].get_summary(state.current_card_task_id)

    formatted_records = []
    for r in records:
        formatted_records.append({
            "index": r['id'],
            "card_display": f"****{r['card_display']}",
            "status": r['status'],
            "bound_to_email": r.get('bound_to_email') or "-",
            "error": r.get('error') or "-",
            "attempt_time": r.get('attempted_at') or "-",
        })

    return jsonify({
        "data": formatted_records,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": summary,
    })


@api.route('/api/card/report')
def download_card_report():
    state = get_app_state()
    models = get_models()

    if not state.current_card_task_id:
        return jsonify({"error": "No report available"}), 404

    records = models['card_binding'].get_all_by_task(state.current_card_task_id)
    report_path = card_service.export_report(records)
    directory = os.path.dirname(report_path)
    filename = os.path.basename(report_path)
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

    # 获取该账号绑定的卡片列表，后续根据页面实际使用的卡片后四位匹配完整卡号
    cards = models['card_binding'].get_by_email(email)

    # 先创建充值记录，card_display 稍后根据实际使用的卡片更新
    log_id = models['recharge_log'].create(email, card_display='', amount=10)

    def _do_recharge():
        try:
            success, err, responses, card_last4 = registration.recharge_account(
                email, cf_password,
                monitor_callback=state._monitor,
            )

            # 用页面提取的后四位匹配完整卡号
            matched_card = ''
            if card_last4:
                for c in cards:
                    if c.get('card_number', '').endswith(card_last4):
                        matched_card = c['card_number']
                        break
                if not matched_card:
                    matched_card = f'•••• {card_last4}'
            if matched_card:
                models['recharge_log'].update_card(log_id, matched_card)
            # 从 API 响应中提取结果
            topup_resp = None
            for resp in responses:
                url = resp.get('url', '')
                if 'topup' in url or 'payment_intents' in url:
                    topup_resp = resp
                    break

            if success and topup_resp:
                resp_data = topup_resp.get('data', {})
                http_status = topup_resp.get('status', 0)
                # CF topup API: success=true 表示成功
                if isinstance(resp_data, dict) and resp_data.get('success') is True:
                    models['recharge_log'].mark_success(log_id, api_response=topup_resp)
                    state.current_action = f"{email} 充值成功"
                    state.add_log(f"{email} AI Credits 充值 $10 成功")
                else:
                    # API 返回了错误（如 409 重复充值）
                    error_msg = ''
                    if isinstance(resp_data, dict):
                        errors = resp_data.get('errors', [])
                        if errors:
                            error_msg = errors[0].get('message', str(resp_data))
                        else:
                            error_msg = str(resp_data)
                    else:
                        error_msg = str(resp_data)
                    models['recharge_log'].mark_failed(log_id, error=f"[HTTP {http_status}] {error_msg}", api_response=topup_resp)
                    state.current_action = f"{email} 充值失败: {error_msg}"
                    state.add_log(f"{email} 充值失败: {error_msg}")
            elif success:
                # 点击成功但未捕获到 API 响应
                models['recharge_log'].mark_success(log_id)
                state.current_action = f"{email} 充值已提交（未捕获响应）"
                state.add_log(f"{email} AI Credits 充值 $10 已提交")
            else:
                models['recharge_log'].mark_failed(log_id, error=err)
                state.current_action = f"{email} 充值失败: {err}"
                state.add_log(f"{email} 充值失败: {err}")
        except Exception as e:
            models['recharge_log'].mark_failed(log_id, error=str(e))
            state.current_action = f"充值异常: {e}"
            state.add_log(f"充值异常: {e}")
        finally:
            state._stop_screenshot_loop()
            state.is_running = False

    threading.Thread(target=_do_recharge, daemon=True).start()
    return jsonify({"status": "started", "email": email})


@api.route('/api/accounts/open-browser', methods=['POST'])
def open_account_browser():
    """打开浏览器查看账号的 Cloudflare 控制台，不执行任何自动操作"""
    state = get_app_state()
    models = get_models()

    if state.is_running:
        return jsonify({"error": "有任务正在运行，请等待完成后再操作"}), 400

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

    state.is_running = True
    state.current_action = f"正在为 {email} 打开浏览器..."
    state._patch_prints()

    import threading

    def _do_open():
        from src.browser.driver import create_driver, login_cloudflare, close_driver
        driver = None
        try:
            driver = create_driver(headless=False, profile_id=email)
            account_id = login_cloudflare(driver, email, cf_password)
            if account_id:
                state.current_action = f"{email} 浏览器已打开，手动关闭浏览器后自动结束"
                state.add_log(f"{email} 浏览器已打开")
            else:
                state.current_action = f"{email} 登录失败"
                state.add_log(f"{email} 打开浏览器登录失败")

            # 等待用户手动关闭浏览器
            import time
            while True:
                try:
                    _ = driver.title
                    time.sleep(2)
                except Exception:
                    break
        except Exception as e:
            state.current_action = f"打开浏览器异常: {e}"
            state.add_log(f"打开浏览器异常: {e}")
        finally:
            if driver:
                try:
                    close_driver(driver)
                except Exception:
                    pass
            state._stop_screenshot_loop()
            state.is_running = False

    threading.Thread(target=_do_open, daemon=True).start()
    return jsonify({"status": "started", "email": email})


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
               "绑卡状态", "绑卡时间"]
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
