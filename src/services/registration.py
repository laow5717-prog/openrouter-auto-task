"""
Cloudflare 注册 & 绑卡核心业务逻辑
"""

import time
import random

from src.config import cfg
from src.utils import generate_random_password, filter_expired_cards
from src.services.email import create_temp_email, wait_for_verification_email
from src.browser.driver import (
    create_driver,
    close_driver,
    fill_signup_form,
    handle_email_verification,
    navigate_to_billing,
    add_credit_card,
    get_bound_card_count,
    login_cloudflare,
    check_and_handle_cf_challenge,
    dismiss_overdue_dialog,
    navigate_to_ai_credits,
    extract_topup_card_last4,
    close_topup_dialog,
    fill_topup_and_confirm,
    handle_unpaid_invoices,
    read_credits_balance,
)
import src.services.captcha as captcha_solver


def register_one_account(db, account_model, card_info_list=None, cf_password=None,
                         monitor_callback=None, max_bindable_cards=2, captcha_api_key=None):
    """
    注册单个 Cloudflare 账号并添加信用卡

    返回: (邮箱, 密码, 是否成功)
    """
    driver = None
    email = None
    password = None
    success = False

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        print("正在创建邮箱...")
        email, email_password, jwt_token = create_temp_email()
        if not email:
            print("创建邮箱失败，终止")
            return None, None, False
        print(f"邮箱密码: {email_password} (登录 mail.tm)")

        if cf_password:
            password = cf_password
            print("使用自定义密码")
        else:
            password = generate_random_password()

        driver = create_driver(headless=False)
        _report("init_browser")

        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key)

        if not fill_signup_form(driver, email, password):
            print("填写注册表单失败")
            return email, password, False
        _report("fill_form")

        time.sleep(5)
        verification_data = wait_for_verification_email(jwt_token)

        if not verification_data:
            print("未收到验证数据，终止")
            return email, password, False

        if not handle_email_verification(driver, verification_data):
            print("邮箱验证失败")
            return email, password, False
        _report("email_verified")

        # 保存到数据库
        account_model.upsert(email, password, email_password, "registered")

        print("\n" + "=" * 50)
        print(f"注册成功！邮箱: {email}")
        print("=" * 50)

        success = True
        print("等待页面稳定...")
        time.sleep(5)
        _report("registered")

        # 导航到账单页面
        print("\n" + "-" * 30)
        print("正在导航到账单页面")
        print("-" * 30)

        if navigate_to_billing(driver):
            print("已进入账单页面")
            account_model.update_status(email, "billing_page")
            _report("billing_page")

            # 添加信用卡（过期/已判无效的卡不再尝试绑定，避免无谓的失败尝试）
            available_cards, _skipped = filter_expired_cards(
                [c for c in (card_info_list or []) if c.get('number')]
            )
            if _skipped:
                print(f"已跳过 {len(_skipped)} 张无效/过期卡")
            if available_cards:
                print("\n" + "-" * 30)
                print("开始绑定信用卡")
                print("-" * 30)

                bound_count = get_bound_card_count(driver)
                cards_added = 0

                for card_idx, card_info in enumerate(available_cards):
                    card_display = card_info['number'][-4:] if len(card_info['number']) >= 4 else card_info['number']
                    print(f"\n正在添加卡 {card_idx + 1}/{len(available_cards)} (尾号 {card_display})...")

                    _success, _err_reason = add_credit_card(driver, card_info)
                    if _success:
                        cards_added += 1
                        bound_count += 1
                        print(f"卡 (尾号 {card_display}) 添加成功！(已绑 {bound_count} 张)")
                        _report("card_added")

                        if card_idx < len(available_cards) - 1:
                            print("返回账单页面...")
                            navigate_to_billing(driver)
                            time.sleep(3)
                    else:
                        print(f"卡 (尾号 {card_display}) 绑定失败，尝试下一张: {_err_reason}")
                        _report("card_failed")
                        if card_idx < len(available_cards) - 1:
                            navigate_to_billing(driver)
                            time.sleep(3)

                if cards_added > 0:
                    account_model.update_status(email, f"bound_{cards_added}_cards")
                else:
                    account_model.update_status(email, "card_bind_failed")
            else:
                print("未提供信用卡信息，跳过绑定")
                _report("no_card_info")
        else:
            print("导航到账单页面失败")
            account_model.update_status(email, "billing_navigation_failed")
            _report("billing_failed")

        success = True
        time.sleep(5)

    except InterruptedError:
        print("任务被用户中断")
        if email:
            account_model.update_status(email, "interrupted")
        return email, password, False

    except Exception as e:
        print(f"错误: {e}")
        if email and password:
            account_model.update_status(email, f"error: {str(e)[:50]}")

    finally:
        if driver:
            print("正在关闭浏览器...")
            close_driver(driver)

    return email, password, success


def register_and_bind_cards(db, account_model, card_binding_model, task_id,
                            batch_records, cf_password=None, max_bindable_cards=2,
                            captcha_api_key=None, monitor_callback=None):
    """
    注册一个账号并逐张绑定信用卡，精细跟踪每张卡的状态

    返回: (email, password, bound_count)
    """
    driver = None
    email = None
    password = None
    bound_count = 0

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        print("正在创建邮箱...")
        email, email_password, jwt_token = create_temp_email()
        if not email:
            print("创建邮箱失败")
            return None, None, 0
        print(f"邮箱密码: {email_password}")

        if cf_password:
            password = cf_password
        else:
            password = generate_random_password()

        driver = create_driver(headless=False)
        _report("init_browser")

        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key)

        if not fill_signup_form(driver, email, password):
            print("注册表单填写失败")
            return email, password, 0
        _report("fill_form")

        time.sleep(5)
        verification_data = wait_for_verification_email(jwt_token)
        if not verification_data:
            print("未收到验证数据")
            return email, password, 0

        if not handle_email_verification(driver, verification_data):
            print("邮箱验证失败")
            return email, password, 0
        _report("email_verified")

        account_model.upsert(email, password, email_password, "registered")
        print(f"注册成功！邮箱: {email}")

        time.sleep(5)
        _report("registered")

        if not navigate_to_billing(driver):
            print("导航到账单页面失败")
            account_model.update_status(email, "billing_navigation_failed")
            return email, password, 0
        print("已进入账单页面")
        _report("billing_page")

        # 逐张绑定信用卡，失败不计数，继续尝试直到绑够 max_bindable_cards
        for idx, record in enumerate(batch_records):
            if bound_count >= max_bindable_cards:
                print(f"账号已达到最大绑卡数 ({max_bindable_cards})，停止绑卡，剩余卡片留待下个账号处理")
                break

            card_info = record["card"]
            card_display = f"****{record['card_display']}"
            binding_id = record["id"]
            print(f"\n绑定 {idx + 1}/{len(batch_records)}: {card_display}...")

            _success, _err_reason = add_credit_card(driver, card_info)
            if _success:
                bound_count += 1
                card_binding_model.mark_success(binding_id, email)
                print(f"{card_display} 绑定成功！(已绑 {bound_count} 张)")
                _report("card_added")

                if idx < len(batch_records) - 1:
                    navigate_to_billing(driver)
                    time.sleep(3)
            else:
                card_binding_model.mark_failed(binding_id, _err_reason or "bind failed")
                print(f"{card_display} 绑定失败，尝试下一张... ({_err_reason})")
                _report("card_failed")
                if idx < len(batch_records) - 1:
                    navigate_to_billing(driver)
                    time.sleep(3)

        if bound_count > 0:
            account_model.update_status(email, f"bound_{bound_count}_cards")
        else:
            account_model.update_status(email, "all_bindings_failed")

    except InterruptedError:
        print("任务被用户中断")
        if email:
            account_model.update_status(email, "interrupted")
    except Exception as e:
        print(f"错误: {e}")
        if email:
            account_model.update_status(email, f"error: {str(e)[:50]}")
    finally:
        if driver:
            print("正在关闭浏览器...")
            close_driver(driver)

    return email, password, bound_count


def recharge_account(email, cf_password, recharge_log_model=None, monitor_callback=None,
                     skip_invoice=False, payment_cards=None,
                     valid_card_model=None, card_pool_model=None, account_model=None):
    """
    登录已有 Cloudflare 账号并充值 AI Credits $10

    参数:
        email: 账号邮箱
        cf_password: CF 密码
        recharge_log_model: RechargeLogModel 实例，用于今日记录查询 + 已成功支付卡统计 + 记账
        monitor_callback: 监控回调 (driver, step_name)
        skip_invoice: 是否跳过 Unpaid invoice 在线支付（未选择支付卡分组时为 True）
        payment_cards: 支付卡数据列表（用于在线支付填写信用卡信息）
        valid_card_model: ValidCardModel，支付成功后记录有效卡（source_type=payment）
        card_pool_model: CardPoolModel，支付成功后把底料卡状态标为 'paid'
        account_model: AccountModel，每笔发票支付成功后记录该账号最新的 Credits 余额
    返回:
        (bool, str, list, str): (是否成功, 错误信息, API 响应列表, 卡片后四位)
    """
    driver = None

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        driver = create_driver(headless=False, profile_id=email)
        _report("init_browser")

        print(f"正在登录账号: {email}")
        account_id = login_cloudflare(driver, email, cf_password)
        if not account_id:
            return False, "登录失败，无法获取 account_id", [], ''
        _report("logged_in")

        # === 账单支付选卡策略 ===
        # 一个 CF 账号最多 20 张 distinct 成功支付的卡（含 Top-up）。
        # 选卡：未满 20 且有新卡 → 优先用新卡凑数；已满 20 或无新卡可提供 → 复用该账号
        # 已支付过、且在当前分组内的卡（轮换、避免连续同一张）。每笔支付成功后记账。
        CARD_CAP = 20
        # 先剔除过期卡（有效期已过），并把它们在底料池里标记为 expired。
        # 上层通常已过滤过一遍，这里是最后一道闸：无论谁调用都不会拿过期卡去支付。
        _all_cards, _expired_cards = filter_expired_cards(payment_cards or [], card_pool_model)
        if _expired_cards:
            print(f"  已跳过 {len(_expired_cards)} 张过期卡（已标记为无效）")
        _invalid_numbers = set()      # 本次任务内被判定为无效的卡（拒付/3DS）
        _paid_numbers = set(recharge_log_model.get_success_card_numbers(email)) if recharge_log_model else set()
        _new_cards = [c for c in _all_cards if c.get('number') not in _paid_numbers]
        _sel = {'new_idx': 0, 'reuse_idx': 0, 'last': None}

        def _get_card():
            # 1) 未满 20 且有新卡 → 用新卡（成功后成为一张新的 distinct 卡）
            while len(_paid_numbers) < CARD_CAP and _sel['new_idx'] < len(_new_cards):
                c = _new_cards[_sel['new_idx']]
                _sel['new_idx'] += 1
                if c.get('number') not in _invalid_numbers:
                    return c
            # 2) 已满 20 或无新卡 → 复用该账号已支付过、且在当前分组内的卡（轮换、避开连续同卡）
            reuse_pool = [c for c in _all_cards
                          if c.get('number') in _paid_numbers
                          and c.get('number') not in _invalid_numbers]
            if not reuse_pool:
                return None
            n = len(reuse_pool)
            for _ in range(n):
                c = reuse_pool[_sel['reuse_idx'] % n]
                _sel['reuse_idx'] += 1
                if n == 1 or c.get('number') != _sel['last']:
                    return c
            return reuse_pool[0]

        def _on_invoice_paid(invoice_id, card, responses, amt, balance=None):
            """每笔发票支付成功后：更新选卡状态 + 记账（recharge_log + valid_cards + card_pool）
            balance: 支付后从 credits 页面读到的账户余额（美元，读取失败为 None）"""
            num = card.get('number', '')
            _sel['last'] = num
            _paid_numbers.add(num)
            if recharge_log_model:
                lid = recharge_log_model.create(email, card_display=num, amount=amt or 0)
                recharge_log_model.mark_success(lid, api_response={
                    'invoice': invoice_id, 'responses': responses, 'balance': balance})
            if account_model and balance is not None:
                try:
                    account_model.update_balance(email, balance)
                except Exception as _e:
                    print(f"  记录余额失败: {_e}")
            if valid_card_model:
                valid_card_model.record(card, source_type='payment', source_email=email)
            if card_pool_model:
                try:
                    card_pool_model.mark_status_by_number(num, 'paid')
                except Exception:
                    pass
            bal_txt = f"，余额 ${balance:.2f}" if balance is not None else ""
            print(f"  已记账: invoice {invoice_id} 由卡 ****{str(num)[-4:]} 支付 ${amt}"
                  f"（该账号累计 {len(_paid_numbers)} 张成功卡）{bal_txt}")

        def _on_invoice_failed(invoice_id, card, reason, card_fault=False):
            """每笔发票支付失败后：记录失败原因；仅当失败归因于卡本身时才把底料卡标为无效。

            card_fault=True（拒付 / 卡过期 / 需要 3DS）→ 卡不可用，标记 invalid 并不再选用；
            card_fault=False（页面超时、元素定位失败、结果未确认等脚本侧问题）→ 不动卡状态，
            否则一次脚本抖动就会误杀一张好卡。
            """
            num = card.get('number', '')
            if card_fault and card_pool_model:
                try:
                    card_pool_model.mark_invalid_by_number(num)
                    _invalid_numbers.add(num)     # 本次任务内后续选卡也立即跳过
                except Exception:
                    pass
            if recharge_log_model:
                try:
                    lid = recharge_log_model.create(email, card_display=num, amount=0)
                    recharge_log_model.mark_failed(lid, error=f"invoice {invoice_id}: {reason}"[:200])
                except Exception:
                    pass
            mark_txt = "，已标记为无效卡" if card_fault else "，卡状态不变（脚本侧失败）"
            print(f"  已记失败: invoice {invoice_id} 卡 ****{str(num)[-4:]} 原因: {reason}{mark_txt}")

        def _record_final_balance():
            """流程收尾：重新加载 credits 页面，读一次最终余额并落库（读不到不影响主流程）"""
            try:
                driver.get(f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits")
                time.sleep(5)
                dismiss_overdue_dialog(driver)
                balance = read_credits_balance(driver)
                if balance is None:
                    print("  未读到最终 Credits 余额")
                    return
                print(f"  账号 {email} 最终 Credits 余额: ${balance:.2f}")
                if account_model:
                    account_model.update_balance(email, balance)
            except Exception as e:
                print(f"  读取最终余额失败: {e}")

        print("正在跳转到 AI Credits 页面并点击充值...")
        success = navigate_to_ai_credits(driver, account_id)
        _report("navigated_to_credits")

        if not success:
            return False, "导航到充值页面或点击 Top-up 按钮失败", [], ''

        # 弹窗已打开，提取卡片后四位并检查今日是否已支付
        card_last4 = extract_topup_card_last4(driver)
        _report("extracted_card")

        if not card_last4:
            print("未能从弹窗提取到信用卡信息，无法继续充值")
            close_topup_dialog(driver)
            return False, "未获取到有效信用卡信息", [], ''

        card_already_used_today = False
        if recharge_log_model:
            card_already_used_today = recharge_log_model.has_today_record(email, card_last4)

        if card_already_used_today:
            if skip_invoice:
                print(f"卡片 ****{card_last4} 今日已有充值记录，且未选择支付卡分组，跳过")
                close_topup_dialog(driver)
                return True, "今日已充值，跳过", [], card_last4
            else:
                print(f"卡片 ****{card_last4} 今日已有充值记录，跳过 Top-up，执行账单支付流程")
                close_topup_dialog(driver)
                time.sleep(2)

                # 直接在当前 credits 页面处理 Unpaid invoices
                invoice_results = handle_unpaid_invoices(
                    driver, get_card=_get_card, on_paid=_on_invoice_paid,
                    on_failed=_on_invoice_failed, account_id=account_id)
                if invoice_results:
                    print(f"Unpaid invoice 处理结果: {invoice_results}")
                    time.sleep(10)
                else:
                    print("未发现 Unpaid invoice")

                _record_final_balance()
                return True, "跳过充值，已处理账单", [], card_last4

        # 今日未支付过，继续充值流程
        print("正在填写充值金额并确认支付...")
        pay_success, responses, card_last4 = fill_topup_and_confirm(driver, amount=10)
        _report("topup_confirmed")

        if pay_success:
            print(f"账号 {email} 充值 $10 已提交")

            if not skip_invoice:
                # 有支付卡分组，等待页面更新后检查 Unpaid invoices
                time.sleep(5)
                print("正在返回 Credits 页面检查 Unpaid invoices...")
                credits_url = f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits"
                driver.get(credits_url)
                time.sleep(5)
                check_and_handle_cf_challenge(driver)
                dismiss_overdue_dialog(driver)
                time.sleep(3)

                invoice_results = handle_unpaid_invoices(
                    driver, get_card=_get_card, on_paid=_on_invoice_paid,
                    on_failed=_on_invoice_failed, account_id=account_id)
                if invoice_results:
                    print(f"Unpaid invoice 处理结果: {invoice_results}")
                    time.sleep(10)
            else:
                print("未选择支付卡分组，跳过 Unpaid invoice 处理")
            _record_final_balance()
        else:
            print(f"账号 {email} 充值确认失败")

        return pay_success, "" if pay_success else "填写金额或确认支付失败", responses or [], card_last4

    except Exception as e:
        print(f"充值过程异常: {e}")
        return False, str(e), [], ''

    finally:
        if driver:
            print("正在关闭浏览器...")
            close_driver(driver)
