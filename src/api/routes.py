"""
Flask API 路由
所有 /api/* 端点
"""

import os
import json
from flask import Blueprint, jsonify, request, send_from_directory

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

    if not state.current_card_task_id:
        return jsonify({"records": [], "summary": {"total": 0, "success": 0, "failed": 0, "pending": 0}})

    records = models['card_binding'].get_all_by_task(state.current_card_task_id)
    summary = models['card_binding'].get_summary(state.current_card_task_id)

    formatted_records = []
    for r in records:
        formatted_records.append({
            "index": r['id'] - 1,
            "card_display": f"****{r['card_display']}",
            "status": r['status'],
            "bound_to_email": r.get('bound_to_email') or "-",
            "error": r.get('error') or "-",
            "attempt_time": r.get('attempted_at') or "-",
        })

    return jsonify({
        "records": formatted_records,
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


@api.route('/api/accounts')
def get_accounts():
    models = get_models()
    accounts = models['account'].get_all(order_desc=True)

    result = []
    for acc in accounts:
        result.append({
            "email": acc['email'],
            "password": acc.get('cf_password') or '',
            "status": acc.get('status') or '',
            "time": acc.get('created_at') or '',
            "email_password": acc.get('email_password') or '',
        })

    return jsonify(result)
