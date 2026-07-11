"""
Flask API 路由
所有 /api/* 端点
"""

import os
import io
import json
from flask import Blueprint, jsonify, request, send_from_directory, send_file

from src.config import cfg
from src.services import card as card_service

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

    base_dir = str(card_service.get_base_dir())
    upload_dir = os.path.join(base_dir, "data", "uploads")
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


@api.route('/api/card/start', methods=['POST'])
def start_card_driven_task():
    state = get_app_state()
    if state.is_running:
        return jsonify({"error": "Task already running"}), 400

    data = request.json or {}
    cf_password = data.get('cf_password', None)
    max_bindable_cards = data.get('max_bindable_cards', 2)
    captcha_api_key = data.get('captcha_api_key', None)

    base_dir = str(card_service.get_base_dir())
    upload_path = os.path.join(base_dir, "data", "uploads", "uploaded_cards.xlsx")
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


@api.route('/api/accounts/<email>/cards')
def get_account_cards(email):
    models = get_models()
    cards = models['card_binding'].get_by_email(email)
    return jsonify(cards)


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
