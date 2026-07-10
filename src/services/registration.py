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
        print("Creating email...")
        email, email_password, jwt_token = create_temp_email()
        if not email:
            print("Failed to create email, aborting")
            return None, None, False
        print(f"Email password: {email_password} (login at mail.tm)")

        if cf_password:
            password = cf_password
            print("Using custom password")
        else:
            password = generate_random_password()

        driver = create_driver(headless=False)
        _report("init_browser")

        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key)

        if not fill_signup_form(driver, email, password):
            print("Failed to fill signup form")
            return email, password, False
        _report("fill_form")

        time.sleep(5)
        verification_data = wait_for_verification_email(jwt_token)

        if not verification_data:
            print("No verification data received, aborting")
            return email, password, False

        if not handle_email_verification(driver, verification_data):
            print("Email verification failed")
            return email, password, False
        _report("email_verified")

        # 保存到数据库
        account_model.upsert(email, password, email_password, "registered")

        print("\n" + "=" * 50)
        print(f"Registration successful! Email: {email}")
        print("=" * 50)

        success = True
        print("Waiting for page to stabilize...")
        time.sleep(5)
        _report("registered")

        # 导航到账单页面
        print("\n" + "-" * 30)
        print("Navigating to billing page")
        print("-" * 30)

        if navigate_to_billing(driver):
            print("Entered billing page")
            account_model.update_status(email, "billing_page")
            _report("billing_page")

            # 添加信用卡
            available_cards = [c for c in (card_info_list or []) if c.get('number')]
            if available_cards:
                print("\n" + "-" * 30)
                print("Starting credit card binding")
                print("-" * 30)

                bound_count = get_bound_card_count(driver)
                cards_added = 0

                for card_idx, card_info in enumerate(available_cards):
                    if bound_count >= max_bindable_cards:
                        print(f"Already bound {bound_count} cards, reached limit ({max_bindable_cards})")
                        break

                    card_display = card_info['number'][-4:] if len(card_info['number']) >= 4 else card_info['number']
                    print(f"\nAdding card {card_idx + 1} (ending {card_display})...")

                    if add_credit_card(driver, card_info):
                        cards_added += 1
                        bound_count += 1
                        print(f"Card (ending {card_display}) added! ({bound_count}/{max_bindable_cards})")
                        _report("card_added")

                        if bound_count >= max_bindable_cards:
                            print(f"Reached limit ({max_bindable_cards})")
                            break

                        print("Returning to billing page...")
                        navigate_to_billing(driver)
                        time.sleep(3)
                    else:
                        print(f"Card (ending {card_display}) failed, trying next")
                        _report("card_failed")
                        navigate_to_billing(driver)
                        time.sleep(3)

                if cards_added > 0:
                    account_model.update_status(email, f"bound_{cards_added}_cards")
                else:
                    account_model.update_status(email, "card_bind_failed")
            else:
                print("No card info provided, skipping binding")
                _report("no_card_info")
        else:
            print("Failed to navigate to billing page")
            account_model.update_status(email, "billing_navigation_failed")
            _report("billing_failed")

        success = True
        time.sleep(5)

    except InterruptedError:
        print("Task interrupted by user")
        if email:
            account_model.update_status(email, "interrupted")
        return email, password, False

    except Exception as e:
        print(f"Error: {e}")
        if email and password:
            account_model.update_status(email, f"error: {str(e)[:50]}")

    finally:
        if driver:
            print("Closing browser...")
            driver.quit()

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
        print("Creating email...")
        email, email_password, jwt_token = create_temp_email()
        if not email:
            print("Failed to create email")
            return None, None, 0
        print(f"Email password: {email_password}")

        if cf_password:
            password = cf_password
        else:
            password = generate_random_password()

        driver = create_driver(headless=False)
        _report("init_browser")

        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key)

        if not fill_signup_form(driver, email, password):
            print("Signup form failed")
            return email, password, 0
        _report("fill_form")

        time.sleep(5)
        verification_data = wait_for_verification_email(jwt_token)
        if not verification_data:
            print("No verification data")
            return email, password, 0

        if not handle_email_verification(driver, verification_data):
            print("Email verification failed")
            return email, password, 0
        _report("email_verified")

        account_model.upsert(email, password, email_password, "registered")
        print(f"Registration successful! Email: {email}")

        time.sleep(5)
        _report("registered")

        if not navigate_to_billing(driver):
            print("Failed to navigate to billing")
            account_model.update_status(email, "billing_navigation_failed")
            return email, password, 0
        print("Entered billing page")
        _report("billing_page")

        # 逐张绑定信用卡
        for record in batch_records:
            if bound_count >= max_bindable_cards:
                break

            card_info = record["card"]
            card_display = f"****{record['card_display']}"
            binding_id = record["id"]
            print(f"\nBinding: {card_display}...")

            if add_credit_card(driver, card_info):
                bound_count += 1
                card_binding_model.mark_success(binding_id, email)
                print(f"{card_display} bound! ({bound_count}/{max_bindable_cards})")
                _report("card_added")

                if bound_count >= max_bindable_cards:
                    break

                navigate_to_billing(driver)
                time.sleep(3)
            else:
                card_binding_model.mark_failed(binding_id, "bind failed")
                print(f"{card_display} binding failed")
                _report("card_failed")
                navigate_to_billing(driver)
                time.sleep(3)

        if bound_count > 0:
            account_model.update_status(email, f"bound_{bound_count}_cards")
        else:
            account_model.update_status(email, "all_bindings_failed")

    except InterruptedError:
        print("Task interrupted by user")
        if email:
            account_model.update_status(email, "interrupted")
    except Exception as e:
        print(f"Error: {e}")
        if email:
            account_model.update_status(email, f"error: {str(e)[:50]}")
    finally:
        if driver:
            print("Closing browser...")
            driver.quit()

    return email, password, bound_count
