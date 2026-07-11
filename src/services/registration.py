"""
Cloudflare 注册 & 绑卡核心业务逻辑
"""

import time
import random

from src.config import cfg
from src.utils import generate_random_password
from src.services.email import create_temp_email, wait_for_verification_email
from src.browser.driver import (
    create_driver,
    close_driver,
    fill_signup_form,
    handle_email_verification,
    navigate_to_billing,
    add_credit_card,
    get_bound_card_count,
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

            # 添加信用卡
            available_cards = [c for c in (card_info_list or []) if c.get('number')]
            if available_cards:
                print("\n" + "-" * 30)
                print("开始绑定信用卡")
                print("-" * 30)

                bound_count = get_bound_card_count(driver)
                cards_added = 0

                for card_idx, card_info in enumerate(available_cards):
                    card_display = card_info['number'][-4:] if len(card_info['number']) >= 4 else card_info['number']
                    print(f"\n正在添加卡 {card_idx + 1}/{len(available_cards)} (尾号 {card_display})...")

                    if add_credit_card(driver, card_info):
                        cards_added += 1
                        bound_count += 1
                        print(f"卡 (尾号 {card_display}) 添加成功！(已绑 {bound_count} 张)")
                        _report("card_added")

                        if card_idx < len(available_cards) - 1:
                            print("返回账单页面...")
                            navigate_to_billing(driver)
                            time.sleep(3)
                    else:
                        print(f"卡 (尾号 {card_display}) 绑定失败，尝试下一张")
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
            card_info = record["card"]
            card_display = f"****{record['card_display']}"
            binding_id = record["id"]
            print(f"\n绑定 {idx + 1}/{len(batch_records)}: {card_display}...")

            if add_credit_card(driver, card_info):
                bound_count += 1
                card_binding_model.mark_success(binding_id, email)
                print(f"{card_display} 绑定成功！(已绑 {bound_count} 张)")
                _report("card_added")

                if idx < len(batch_records) - 1:
                    navigate_to_billing(driver)
                    time.sleep(3)
            else:
                card_binding_model.mark_failed(binding_id, "bind failed")
                print(f"{card_display} 绑定失败，尝试下一张...")
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
