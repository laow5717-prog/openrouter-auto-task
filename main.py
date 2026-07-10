"""
Cloudflare 自动注册 & 绑卡工具
主程序入口

功能:
    - 自动创建临时邮箱
    - 自动完成 Cloudflare 注册流程
    - 自动验证邮箱
    - 导航到管理账户 > 账单页面
    - 自动添加信用卡
"""

import time
import random

from config import (
    TOTAL_ACCOUNTS,
    BATCH_INTERVAL_MIN,
    BATCH_INTERVAL_MAX,
)
from utils import generate_random_password, save_to_txt, update_account_status
from email_service import create_temp_email, wait_for_verification_email
from browser import (
    create_driver,
    fill_signup_form,
    handle_email_verification,
    navigate_to_billing,
    add_credit_card,
)


def register_one_account(card_info=None, monitor_callback=None):
    """
    注册单个 Cloudflare 账号并添加信用卡

    参数:
        card_info: 信用卡信息字典（由 Web UI 传入），包含:
            - number: 卡号
            - expiry_month: 有效期月 (MM)
            - expiry_year: 有效期年 (YYYY)
            - cvc: 安全码
            - name: 持卡人姓名（可选）
            - address, city, state, zip, country: 账单地址（可选）
        monitor_callback: 回调函数 func(driver, step_name)，用于截图和中断检查

    返回:
        tuple: (邮箱, 密码, 是否成功)
    """
    driver = None
    email = None
    password = None
    success = False

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        # 1. 创建邮箱
        print("📧 正在创建邮箱...")
        email, email_password, jwt_token = create_temp_email()
        if not email:
            print("❌ 创建邮箱失败，终止注册")
            return None, None, False
        print(f"📧 邮箱密码: {email_password}（可在 mail.tm 网站登录查看邮件）")

        # 2. 生成随机密码
        password = generate_random_password()

        # 3. 初始化浏览器
        driver = create_driver(headless=False)
        _report("init_browser")

        # 4. 填写注册表单
        if not fill_signup_form(driver, email, password):
            print("❌ 填写注册表单失败")
            return email, password, False
        _report("fill_form")

        # 5. 等待验证邮件
        time.sleep(5)
        verification_data = wait_for_verification_email(jwt_token)

        if not verification_data:
            print("❌ 未获取到验证信息，终止注册")
            return email, password, False

        # 6. 处理邮箱验证
        if not handle_email_verification(driver, verification_data):
            print("❌ 邮箱验证失败")
            return email, password, False
        _report("email_verified")

        # 7. 保存账号信息（注册成功）
        save_to_txt(email, password, "已注册", email_password=email_password)

        print("\n" + "=" * 50)
        print("🎉 注册成功！")
        print(f"   邮箱: {email}")
        print(f"   密码: {password}")
        print("=" * 50)

        success = True
        print("⏳ 等待页面稳定...")
        time.sleep(5)
        _report("registered")

        # 8. 导航到账单页面
        print("\n" + "-" * 30)
        print("🚀 正在导航到账单页面")
        print("-" * 30)

        if navigate_to_billing(driver):
            print("✅ 已进入账单页面")
            update_account_status(email, "已进入账单页面")
            _report("billing_page")

            # 9. 添加信用卡（如果提供了卡片信息）
            if card_info and card_info.get('number'):
                print("\n" + "-" * 30)
                print("💳 正在添加信用卡")
                print("-" * 30)

                if add_credit_card(driver, card_info):
                    print("🎉 信用卡添加成功！")
                    update_account_status(email, "已添加信用卡")
                    _report("card_added")
                else:
                    print("⚠️ 信用卡添加失败")
                    update_account_status(email, "信用卡添加失败")
                    _report("card_failed")
            else:
                print("ℹ️ 未提供信用卡信息，跳过绑卡步骤")
                _report("no_card_info")
        else:
            print("⚠️ 导航到账单页面失败")
            update_account_status(email, "账单页面导航失败")
            _report("billing_failed")

        success = True
        time.sleep(5)

    except InterruptedError:
        print("🛑 任务已被用户强制中断")
        if email:
            update_account_status(email, "用户中断")
        return email, password, False

    except Exception as e:
        print(f"❌ 发生错误: {e}")
        if email and password:
            update_account_status(email, f"错误: {str(e)[:50]}")

    finally:
        if driver:
            print("🔒 正在关闭浏览器...")
            driver.quit()

    return email, password, success


def run_batch(card_info=None):
    """
    批量注册账号

    参数:
        card_info: 信用卡信息字典（可选）
    """
    print("\n" + "=" * 60)
    print(f"🚀 开始批量注册，目标数量: {TOTAL_ACCOUNTS}")
    print("=" * 60 + "\n")

    print("\n⚠️ 免责声明：本项目仅供学习研究使用。请勿用于商业用途或违规操作。")
    print("⚠️ 使用者需自行承担因违规使用导致的一切后果。\n")
    time.sleep(2)

    success_count = 0
    fail_count = 0
    registered_accounts = []

    for i in range(TOTAL_ACCOUNTS):
        print("\n" + "#" * 60)
        print(f"📝 正在注册第 {i + 1}/{TOTAL_ACCOUNTS} 个账号")
        print("#" * 60 + "\n")

        email, password, success = register_one_account(card_info=card_info)

        if success:
            success_count += 1
            registered_accounts.append((email, password))
        else:
            fail_count += 1

        # 显示进度
        print("\n" + "-" * 40)
        print(f"📊 当前进度: {i + 1}/{TOTAL_ACCOUNTS}")
        print(f"   ✅ 成功: {success_count}")
        print(f"   ❌ 失败: {fail_count}")
        print("-" * 40)

        # 如果还有下一个，等待随机时间
        if i < TOTAL_ACCOUNTS - 1:
            wait_time = random.randint(BATCH_INTERVAL_MIN, BATCH_INTERVAL_MAX)
            print(f"\n⏳ 等待 {wait_time} 秒后继续下一个注册...")
            time.sleep(wait_time)

    # 最终统计
    print("\n" + "=" * 60)
    print("🏁 批量注册完成")
    print("=" * 60)
    print(f"   总计: {TOTAL_ACCOUNTS}")
    print(f"   ✅ 成功: {success_count}")
    print(f"   ❌ 失败: {fail_count}")

    if registered_accounts:
        print("\n📋 成功注册的账号:")
        for email, password in registered_accounts:
            print(f"   - {email}")

    print("=" * 60)


if __name__ == "__main__":
    run_batch()
