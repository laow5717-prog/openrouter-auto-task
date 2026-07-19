"""
Cloudflare 注册 & 绑卡核心业务逻辑
"""

import time
import random

from src.config import cfg, TOPUP_AMOUNT, INVOICE_DAILY_CAP
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
    reset_credit_balance,
    wait_for_credit_balance,
    extract_decline_from_responses,
    fetch_today_invoice_count,
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
                    account_model.update_bound_cards(email, cards_added)
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
                            captcha_api_key=None, monitor_callback=None,
                            claim_more=None):
    """
    注册一个账号并逐张绑定信用卡，精细跟踪每张卡的状态

    claim_more: 可选回调 claim_more(n) -> [record, ...]。绑定失败的卡不占用
        max_bindable_cards 名额，卡试完仍未绑够时用它再领一批继续（最多 3 轮）。
        不传则维持原行为：batch_records 用完即结束。

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

        # 逐张绑定信用卡。失败的卡不计入 bound_count，也不占用 max_bindable_cards 名额：
        # 队列可在中途追加，卡都试完仍未绑够时再领一批，复用当前浏览器继续。
        queue = list(batch_records)
        idx = 0
        extra_rounds = 0
        while idx < len(queue):
            if bound_count >= max_bindable_cards:
                print(f"账号已达到最大绑卡数 ({max_bindable_cards})，停止绑卡，剩余卡片留待下个账号处理")
                break

            record = queue[idx]
            card_info = record["card"]
            card_display = f"****{record['card_display']}"
            binding_id = record["id"]
            print(f"\n绑定 {idx + 1}/{len(queue)}: {card_display}...")

            _success, _err_reason = add_credit_card(driver, card_info)
            if _success:
                bound_count += 1
                card_binding_model.mark_success(binding_id, email)
                print(f"{card_display} 绑定成功！(已绑 {bound_count} 张)")
                _report("card_added")
            else:
                card_binding_model.mark_failed(binding_id, _err_reason or "bind failed")
                print(f"{card_display} 绑定失败 ({_err_reason})")
                _report("card_failed")

            idx += 1
            still_need = max_bindable_cards - bound_count

            # 卡试完但没绑够 → 再领一批。失败的卡不该消耗名额，否则一张卡被拒
            # 就永远达不到目标（本批只领了 max_bindable_cards 张）。
            if idx >= len(queue) and still_need > 0 and claim_more and extra_rounds < 3:
                extra = claim_more(still_need) or []
                extra_rounds += 1
                seen_ids = {r["id"] for r in queue}
                fresh = [r for r in extra if r["id"] not in seen_ids]
                if fresh:
                    queue.extend(fresh)
                    print(f"仍需 {still_need} 张，已再领 {len(fresh)} 张继续尝试")

            if idx < len(queue) and bound_count < max_bindable_cards:
                navigate_to_billing(driver)
                time.sleep(3)
            elif bound_count < max_bindable_cards:
                print(f"已无可用卡片，本账号绑卡结束（已绑 {bound_count}/{max_bindable_cards} 张）")

        if bound_count > 0:
            account_model.update_bound_cards(email, bound_count)
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


def _get_email_password(account_model, email):
    """取邮箱密码供登录二次验证使用。

    account_model 在部分调用路径上是可选的（可能为 None），且老账号未必存了
    邮箱密码——两种情况都返回 None，此时登录遇到 2FA 页会失败，与改动前行为一致。
    """
    if not account_model or not email:
        return None
    try:
        return account_model.get_email_password(email)
    except Exception as e:
        print(f"  读取邮箱密码失败: {e}")
        return None


def bind_cards_to_existing_account(account_model, card_binding_model, task_id,
                                   email, cf_password, batch_records,
                                   max_bindable_cards=2, captcha_api_key=None,
                                   monitor_callback=None, claim_more=None):
    """登录已有 Cloudflare 账号并补绑信用卡，补到账号总绑卡数达 max_bindable_cards。

    与 register_and_bind_cards 的区别：跳过注册，直接登录已有账号。以账单页真实
    已绑卡数（get_bound_card_count）决定还需补几张，避免超绑。

    claim_more: 可选回调 claim_more(n) -> [record, ...]，用于「卡都试完仍未补够」时
        再领一批。传入才启用，最多追加 3 轮，避免单个账号把卡池吃光。不传则维持
        原行为：batch_records 用完即结束。

    返回: (bound_count, login_ok)
      - login_ok=False → bound_count=0，且 batch_records 里的卡未被消耗（保留 pending），
        由上层跳过该账号、把卡留给下一个账号。
      - login_ok=True 但 bound_count=0 → 账号已满或账单页异常，视为已处理，卡不消耗。
    """
    driver = None
    bound_count = 0

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        driver = create_driver(headless=False, profile_id=email)
        _report("init_browser")

        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key)

        print(f"正在登录账号: {email}")
        account_id = login_cloudflare(
            driver, email, cf_password, _get_email_password(account_model, email))
        if not account_id:
            print("登录失败，跳过该账号（卡片保留待下个账号处理）")
            return 0, False
        _report("logged_in")

        # 登录过程中弹出过欠费/待支付弹窗 = 该账号已产生账单 = 已绑过卡。
        # 此时只核对账单页真实卡数并回写账号，不再补绑：这些卡多半不在卡池里，
        # 库里查不到也不做关联（card_bindings 不写），仅以 bound_card_count 体现。
        already_bound = getattr(driver, "overdue_dialog_seen", False)

        if not navigate_to_billing(driver):
            print("导航到账单页面失败，跳过该账号")
            return 0, True
        _report("billing_page")

        # 以页面真实已绑卡数决定还需补几张，避免超绑
        current = get_bound_card_count(driver)
        if current is None:
            current = 0
        # 无论走哪条分支，页面读数都是权威值，先落库
        account_model.update_bound_cards(email, current)

        if already_bound:
            print(f"账号 {email} 登录时出现待支付弹窗，判定为已绑卡账号；"
                  f"账单页核对到 {current} 张，已更新账号信息，跳过补绑")
            _report("cards_checked")
            return 0, True

        need = max_bindable_cards - current
        print(f"账号 {email} 当前已绑 {current} 张，目标 {max_bindable_cards} 张，需补 {max(need, 0)} 张")
        if need <= 0:
            print("账号已达目标绑卡数，无需补绑")
            return 0, True

        # 队列可在中途追加：卡都试完但还没补够时，向上层再领一批继续，
        # 复用当前已登录的浏览器（重开浏览器要再登录一次，代价高得多）。
        queue = list(batch_records)
        idx = 0
        extra_rounds = 0
        while idx < len(queue):
            if bound_count >= need:
                print(f"已补满 {need} 张，剩余卡片留待下个账号处理")
                break

            record = queue[idx]
            card_info = record["card"]
            card_display = f"****{record['card_display']}"
            binding_id = record["id"]
            print(f"\n补绑 {idx + 1}: {card_display}...")

            _success, _err_reason = add_credit_card(driver, card_info)
            if _success:
                bound_count += 1
                card_binding_model.mark_success(binding_id, email)
                print(f"{card_display} 绑定成功！(本轮已绑 {bound_count} 张)")
                _report("card_added")
            else:
                card_binding_model.mark_failed(binding_id, _err_reason or "bind failed")
                print(f"{card_display} 绑定失败 ({_err_reason})")
                _report("card_failed")

            idx += 1
            still_need = need - bound_count

            # 卡试完但没补够 → 再领一批。限制轮数，避免某账号把整个卡池吃光。
            if idx >= len(queue) and still_need > 0 and claim_more and extra_rounds < 3:
                extra = claim_more(still_need) or []
                extra_rounds += 1
                # claim_batch 返回该 worker 名下所有 processing 记录。已处理的卡此时
                # 已转为 success/failed 不会回流，但仍按 id 去重，避免任何情况下重复绑同一张。
                seen_ids = {r["id"] for r in queue}
                fresh = [r for r in extra if r["id"] not in seen_ids]
                if fresh:
                    queue.extend(fresh)
                    print(f"仍需 {still_need} 张，已再领 {len(fresh)} 张继续尝试")

            if idx < len(queue) and bound_count < need:
                print("准备下一张...")
                navigate_to_billing(driver)
                time.sleep(3)
            elif bound_count < need:
                # 说明「没卡了」而不是「不想试了」——此前这里无条件打印「尝试下一张」
                # 然后直接关浏览器，日志与实际行为矛盾
                print(f"已无可用卡片，本账号补绑结束（still_need={still_need}）")

        if bound_count > 0:
            account_model.update_bound_cards(email, current + bound_count)

    except InterruptedError:
        print("任务被用户中断")
        raise
    except Exception as e:
        print(f"补绑出错: {e}")
        return bound_count, True
    finally:
        if driver:
            print("正在关闭浏览器...")
            close_driver(driver)

    return bound_count, True


def recharge_account(email, cf_password, recharge_log_model=None, monitor_callback=None,
                     skip_invoice=False, payment_cards=None,
                     valid_card_model=None, card_pool_model=None, account_model=None,
                     should_stop=None, card_binding_model=None, card_state_model=None,
                     invoice_daily_cap=None, invoice_state_model=None,
                     payment_registry=None):
    """
    登录已有 Cloudflare 账号并充值 AI Credits $10

    参数:
        email: 账号邮箱
        cf_password: CF 密码
        recharge_log_model: RechargeLogModel 实例，用于今日记录查询 + 已成功支付卡统计 + 记账
        monitor_callback: 监控回调 (driver, step_name)
        skip_invoice: 是否跳过 Unpaid invoice 在线支付（未选择支付卡分组时为 True）
        payment_cards: 支付卡数据列表（用于在线支付填写信用卡信息）
        payment_registry: PaymentCardRegistry，并行执行时用于在多个 worker 之间
                  排他占用支付卡。为 None（串行路径）时选卡行为与从前逐行一致。
        valid_card_model: ValidCardModel，支付成功后记录有效卡（source_type=payment）
        card_pool_model: CardPoolModel，支付成功后把底料卡状态标为 'paid'
        account_model: AccountModel，每笔发票支付成功后记录该账号最新的 Credits 余额
        card_binding_model: CardBindingModel，Top-up 因卡本身被拒时把对应绑定卡标记失效
        invoice_state_model: InvoicePaymentStateModel，账单支付页出现「此账单已无法在 Stripe
                             支付」时标记该发票 24h 冷却；后续充值在冷却期内跳过该发票、转付新账单
        invoice_daily_cap: None＝现状全量模式（返回 5 元组）。传整数（如 30）＝单步(round-robin)
                           模式：先读该账号当日账单数，未达上限则做 1 次 Top-up 生成账单 +
                           至多付 1 张，随后返回，供编排层切换下一个账号。返回 6 元组。
    返回:
        全量模式 (invoice_daily_cap is None)：
            (bool, str, list, str, str): (是否成功, 错误信息, API 响应列表, 卡片后四位, 结局)
            结局(outcome): "topup"=实际执行了充值; "invoice_only"=未充值仅处理/检查账单;
                           "failed"=失败或异常。调用方据此记账：invoice_only 不得记为充值成功。
        单步模式 (invoice_daily_cap 为整数)：
            (bool, str, list, str, str, dict): 末位 info 含
            {today_count, generated(bool), paid(int), topup_ok(bool)}；
            outcome 取值："cap_reached"=当日已达上限未操作; "stepped"=做了 1 生成+至多 1 付;
                          "failed"=失败或异常。
    """
    driver = None
    # 本账号在 payment_registry 中占住的卡号，供 finally 统一释放。
    # 定义在 try 之前：任何早期异常也必须能走到释放逻辑。
    _claimed_numbers = set()

    def _report(step_name):
        if monitor_callback and driver:
            monitor_callback(driver, step_name)

    try:
        driver = create_driver(headless=False, profile_id=email)
        _report("init_browser")

        print(f"正在登录账号: {email}")
        account_id = login_cloudflare(
            driver, email, cf_password, _get_email_password(account_model, email))
        if not account_id:
            if invoice_daily_cap is not None:
                return False, "登录失败，无法获取 account_id", [], '', "failed", {'today_count': None}
            return False, "登录失败，无法获取 account_id", [], '', "failed"
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

        # === 选卡资格闸门（R1 一卡绑一账号 / R2 单卡24h≤2次 / R3 3DS临时冷却）===
        # 在既有"新卡优先/复用"逻辑之前先过滤，被排除的按原因计数打印（可见性）。
        def _eligible(num):
            if not num:
                return False, "无卡号"
            if valid_card_model:
                bound = valid_card_model.get_bound_email(num)
                if bound and bound != email:
                    return False, "已绑定其他账号"
            if recharge_log_model and recharge_log_model.success_count_since(num, 24) >= 2:
                return False, "24h内已支付2次(冷却中)"
            if card_state_model and card_state_model.in_tds_cooldown(num):
                return False, "3DS临时冷却中"
            return True, ""

        _skip_reasons = {}
        _eligible_cards = []
        for c in _all_cards:
            ok, why = _eligible(c.get('number', ''))
            if ok:
                _eligible_cards.append(c)
            else:
                _skip_reasons[why] = _skip_reasons.get(why, 0) + 1
        if _skip_reasons:
            print("  选卡规则跳过: " + "，".join(f"{k}×{v}" for k, v in _skip_reasons.items()))
        _all_cards = _eligible_cards

        _invalid_numbers = set()      # 本次任务内被判定为无效的卡（拒付/3DS）
        _paid_numbers = set(recharge_log_model.get_success_card_numbers(email)) if recharge_log_model else set()
        _new_cards = [c for c in _all_cards if c.get('number') not in _paid_numbers]
        _sel = {'new_idx': 0, 'reuse_idx': 0, 'last': None}

        # 本次选卡里因「被别的 worker 占着」而没能用上的卡数。用来区分两种 None：
        # 真的没卡可用（卡池耗尽，应放弃该账号） vs 只是这一刻被占（稍后重试即可）。
        _contended = {'count': 0}

        def _claim(card):
            """并发下占住这张卡；payment_registry 为 None（串行路径）时直接放行。

            上面的资格闸门（_eligible）全部从 DB 实时派生，且 _eligible_cards 是
            进入本函数时的一次性快照。并发时两个 worker 会同时把同一张卡判为合格，
            各自拿去给不同账号支付，等 DB 记录写下时"一卡绑一账号"已被违反。
            payment_registry 补的就是"判定合格"到"写入结果"之间的时间差窗口。

            占用**只覆盖单次支付尝试**，支付有结果后立刻释放（见 _release_card）。
            早先的实现是占到整个账号处理结束，结果在卡池偏紧时把其它 worker 饿死，
            对方误判成「卡池已耗尽」而放弃整个账号。按笔释放是安全的：一旦某张卡
            成功支付，valid_cards 会记下绑定关系，_eligible 就会把它挡在其它账号
            之外——DB 规则接管了长期约束，内存登记只需守住支付进行中的那一小段。"""
            if card is None or payment_registry is None:
                return card
            num = card.get('number', '')
            if payment_registry.try_acquire(num, email):
                _claimed_numbers.add(num)
                return card
            _contended['count'] += 1
            return None                    # 被其它 worker 占用 → 本次跳过，但不算无效

        def _release_card(card):
            """一笔支付有结果后释放占用，让其它 worker 能立刻用上这张卡。"""
            if card is None or payment_registry is None:
                return
            num = card.get('number', '')
            payment_registry.release(num)
            _claimed_numbers.discard(num)

        def _get_card():
            _contended['count'] = 0
            # 1) 未满 20 且有新卡 → 用新卡（成功后成为一张新的 distinct 卡）
            #    注意 new_idx 只在卡被真正取用或判定无效时才前进：若只是被别的 worker
            #    暂时占着，必须把它留在原地，否则这张卡在本账号会话里就永久丢了。
            while len(_paid_numbers) < CARD_CAP and _sel['new_idx'] < len(_new_cards):
                c = _new_cards[_sel['new_idx']]
                if c.get('number') in _invalid_numbers:
                    _sel['new_idx'] += 1
                    continue
                got = _claim(c)
                if got is not None:
                    _sel['new_idx'] += 1
                    return got
                break            # 被占用 → 保持 new_idx 不动，等下次重试这张
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
                    got = _claim(c)
                    if got is not None:
                        return got
            return _claim(reuse_pool[0])

        def _cards_only_contended():
            """本次没取到卡，是否**纯粹**因为被其它 worker 占着？

            调用方据此决定：真耗尽 → 放弃该账号；只是争用 → 下一轮再来。"""
            return _contended['count'] > 0

        def _on_invoice_paid(invoice_id, card, responses, amt, balance=None):
            """每笔发票支付成功后：更新选卡状态 + 记账（recharge_log + valid_cards + card_pool）
            balance: 支付后从 credits 页面读到的账户余额（美元，读取失败为 None）"""
            num = card.get('number', '')
            print(f"  [记账] 进入 on_paid: invoice {invoice_id} 卡 ****{str(num)[-4:]} "
                  f"金额 ${amt} 余额 {balance}")
            _sel['last'] = num
            _paid_numbers.add(num)
            # 各步独立兜异常：任一步失败不应中断其余记账（否则卡号/成功标记会漏记）
            if recharge_log_model:
                try:
                    lid = recharge_log_model.create(email, card_display=num, amount=amt or 0)
                    recharge_log_model.mark_success(lid, api_response={
                        'invoice': invoice_id, 'responses': responses, 'balance': balance})
                    print(f"  [记账] recharge_log 已写入并标记成功 (id={lid})")
                except Exception as _e:
                    import traceback
                    print(f"  [记账] recharge_log 写入失败: {_e}\n{traceback.format_exc()}")
            if account_model and balance is not None:
                try:
                    account_model.update_balance(email, balance)
                except Exception as _e:
                    print(f"  记录余额失败: {_e}")
            if valid_card_model:
                try:
                    valid_card_model.record(card, source_type='payment', source_email=email)
                    print(f"  [记账] valid_card 已记录 ****{str(num)[-4:]}")
                except Exception as _e:
                    import traceback
                    print(f"  [记账] valid_card 记录失败: {_e}\n{traceback.format_exc()}")
            if card_pool_model:
                try:
                    card_pool_model.mark_status_by_number(num, 'paid')
                    print(f"  [记账] card_pool 已标记 ****{str(num)[-4:]} = paid")
                except Exception as _e:
                    print(f"  [记账] card_pool 标记 paid 失败: {_e}")
            bal_txt = f"，余额 ${balance:.2f}" if balance is not None else ""
            print(f"  已记账: invoice {invoice_id} 由卡 ****{str(num)[-4:]} 支付 ${amt}"
                  f"（该账号累计 {len(_paid_numbers)} 张成功卡）{bal_txt}")
            # 记账已落库（valid_cards 记下了绑定关系），长期约束交给 _eligible，
            # 内存占用可以放了——继续占着只会饿死其它 worker
            _release_card(card)

        def _on_invoice_failed(invoice_id, card, reason, card_fault=False, tds=False):
            """每笔发票支付失败后：记录失败原因；仅当失败归因于卡本身时才把底料卡标为无效。

            card_fault=True（拒付 / 卡过期 / 需要 3DS）→ 卡不可用，标记 invalid 并不再选用；
            card_fault=False（页面超时、元素定位失败、结果未确认等脚本侧问题）→ 不动卡状态，
            否则一次脚本抖动就会误杀一张好卡。

            R3 例外：tds=True（3DS）且该卡**曾支付成功**（已绑定）→ 标"临时3DS冷却24h"而非永久
            作废，冷却到期自动恢复；本轮仍加入 _invalid_numbers 以跳过（无法完成 3DS）。"""
            num = card.get('number', '')
            was_successful = bool(valid_card_model and valid_card_model.get_bound_email(num))
            if tds and was_successful and card_state_model:
                try:
                    card_state_model.set_tds(num, hours=24, reason=reason)
                    _invalid_numbers.add(num)     # 本轮跳过，但不永久作废
                    print(f"  卡 ****{str(num)[-4:]} 曾成功、本次 3DS → 临时冷却24h（未永久作废）")
                except Exception:
                    pass
            elif card_fault and card_pool_model:
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
            # 本次尝试已有结论，释放占用。卡若确实有问题，已进 _invalid_numbers /
            # 底料池 invalid 标记，其它 worker 的 _eligible 会挡住它
            _release_card(card)

        def _skip_invoice_cooldown(invoice_id):
            """该发票是否在「无法支付」24h 冷却期内 → 选发票时跳过它，转去支付新账单。"""
            if not invoice_state_model:
                return False
            try:
                return invoice_state_model.in_cooldown(invoice_id)
            except Exception:
                return False

        def _on_invoice_unpayable(invoice_id, pay_url, amt, permanent=False, reason=''):
            """支付页出现「此账单已无法在 Stripe 支付」→ 标 24h 冷却，后续充值不再重复请求。
            permanent=True（如支付页跳转 Stripe 登录页，订单已彻底无效）→ 标 10 年，
            等同永久跳过、以后不再对该发票发起支付。"""
            if not invoice_state_model:
                return
            try:
                hours = 24 * 365 * 10 if permanent else 24
                invoice_state_model.mark_unpayable(
                    invoice_id, email=email, hours=hours,
                    reason=(reason or 'Stripe: invoice can no longer be paid'),
                    pay_url=pay_url or '')
                scope_txt = "永久" if permanent else "24h 内"
                print(f"  已标记 invoice {invoice_id} 无法支付，{scope_txt}跳过该发票")
            except Exception as e:
                print(f"  标记账单无法支付失败: {str(e)[:80]}")

        def _balance_on_credits_page(timeout_ms=8000):
            """当前页应已处于 credits 页：优先取被动监听（page.on("response")）捕获的最新
            credit-balance 余额，未捕获再主动 fetch 兜底。返回 float | None。
            注意：调用方须在导航到 credits 页「之前」先 reset_credit_balance(driver)，
            以免拿到上次页面残留的余额。"""
            b = wait_for_credit_balance(driver, timeout_ms=timeout_ms)  # 被动监听优先
            if b is None:
                b = read_credits_balance(driver)                        # 主动 fetch 兜底
            return b

        def _persist_balance(balance, label):
            """把余额落库（读不到或无 model 时静默跳过），返回原值便于链式使用。"""
            if balance is not None and account_model:
                try:
                    account_model.update_balance(email, balance)
                    print(f"  账号 {email} {label} Credits 余额已更新: ${balance:.2f}")
                except Exception as e:
                    print(f"  更新余额失败: {str(e)[:80]}")
            return balance

        def _record_final_balance():
            """流程收尾：重新加载 credits 页面，读一次最终余额并落库（读不到不影响主流程）。
            余额优先走「被动监听」——credits 页加载时页面自行请求 credit-balance 接口，
            直接抓其响应；被动未捕获再退化为主动 fetch 兜底。返回读到的余额（float）或 None。"""
            try:
                reset_credit_balance(driver)
                driver.get(f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits")
                dismiss_overdue_dialog(driver)
                balance = _balance_on_credits_page()
                if balance is None:
                    print("  未读到最终 Credits 余额")
                    return None
                return _persist_balance(balance, "最终")
            except Exception as e:
                print(f"  读取最终余额失败: {e}")
                return None

        def _topup_confirm_requires_action(responses):
            """topup 的 Stripe confirm 是否返回 requires_action（3DS）。
            用于决定是否给余额一个「无感 3DS 后台结算」的等待窗口。"""
            for r in (responses or []):
                u = r.get('url', '') or ''
                if 'stripe.com' in u and 'confirm' in u:
                    data = r.get('data') if isinstance(r.get('data'), dict) else {}
                    if data.get('object') == 'payment_intent':
                        return data.get('status') == 'requires_action'
            return False

        def _settle_topup_balance(baseline, responses, initial):
            """confirm=requires_action 时，无感 3DS 会在后台结算、余额随后才增长——
            在**卡池卡介入之前**给余额一个短暂结算窗口，让 requires_action 的 topup 能凭
            余额增长被正确判为成功（归属仍干净：此刻尚未用卡池卡付任何账单）。
            余额走 credit-balance 接口（非 DOM），重复读实时且廉价。
            返回结算窗口内读到的最佳（最高）余额；非 requires_action 或已增长则原样返回。"""
            if initial is not None and baseline is not None and initial > baseline + 0.001:
                return initial            # 已增长，无需等待
            if not _topup_confirm_requires_action(responses):
                return initial            # 非无感 3DS，不额外等待（真实拒付等由 confirm 判）
            if baseline is None:
                return initial
            best = initial if initial is not None else baseline
            for i in range(6):            # 最多 ~30s（6 × 5s），等无感 3DS 后台结算
                time.sleep(5)
                try:
                    b = read_credits_balance(driver)
                except Exception:
                    b = None
                if b is not None:
                    best = max(best, b)
                    if b > baseline + 0.001:
                        print(f"  Top-up 无感 3DS 结算窗口第 {i + 1} 次：余额已增长至 ${b:.2f}（基线 ${baseline:.2f}）")
                        return b
            print(f"  Top-up 无感 3DS 结算窗口结束：余额仍未增长（最佳 ${best:.2f}，基线 ${baseline:.2f}）")
            return best

        def _classify_topup(responses, baseline, post_topup):
            """判定 Top-up 的**真实**结果，返回 (ok: bool, reason: str, card_fault: bool)。

            权威源是 Stripe payment_intents/confirm 响应，而非 CF topup（200/success 仅表示
            已创建支付意图）。confirm 未捕获时退化为余额兜底：topup 后余额较基线增长才算成功，
            读不到余额则保守判失败（宁漏记成功不误记成功）。"""
            stripe = None
            for r in (responses or []):
                u = r.get('url', '') or ''
                if 'stripe.com' in u and 'confirm' in u:
                    stripe = r
                    break
            if stripe is not None:
                status = stripe.get('status', 0) or 0
                data = stripe.get('data') if isinstance(stripe.get('data'), dict) else {}
                has_error = bool(data.get('error'))
                pi_status = data.get('status') if data.get('object') == 'payment_intent' else None
                if int(status) >= 400 or has_error:
                    # 从冻结的 responses 提取原因（不依赖 live net_responses，后者已被账单流程清空）
                    reason, card_fault = extract_decline_from_responses(responses)
                    return False, (reason or '支付被拒'), (card_fault if reason else True)
                if pi_status in ('succeeded', 'processing', 'requires_capture'):
                    return True, '', False
                # requires_action：可能是 3DS2 fingerprint 无感验证（会在后台自动完成扣款、
                # 账单随即变 Paid），也可能是需用户交互的真实 3DS（无法自动完成）。二者无法从
                # confirm 响应本身区分，故不再一律判失败——交由余额兜底权威判定：余额较基线增长
                # 即已扣款成功（无感 3DS 已完成），否则才判失败。误判失败会漏记成功卡、且把已扣款
                # 账单当欠费重复处理。
                if pi_status == 'requires_action':
                    if baseline is not None and post_topup is not None:
                        if post_topup > baseline + 0.001:
                            print(f"  Top-up confirm=requires_action，但余额已增长"
                                  f"（${baseline:.2f}→${post_topup:.2f}）→ 判定 3DS 无感验证已完成、扣款成功")
                            return True, '', False
                        return False, '需 3DS 验证且余额未增长（无感验证未完成）', False
                    # 读不到余额 → 无法确认，保守判失败（宁漏记成功不误记成功）
                    return False, '需 3DS 验证且余额未知（无法确认）', False
                # 捕获到 confirm 但状态不明确（其它中间态）→ 保守判失败
                return False, f'支付状态未确认（{pi_status or status}）', False
            # 未捕获到 Stripe confirm → 余额兜底
            if baseline is not None and post_topup is not None:
                if post_topup > baseline + 0.001:
                    return True, '', False
                return False, '充值后余额未增长（未捕获支付结果）', False
            return False, '未捕获支付结果且余额未知', False

        credits_url = f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits"

        # ============================================================
        # 单步（round-robin）模式：读当日账单数 → 至多 1 次 Top-up 生成账单 → 至多付 1 张。
        # 达当日上限则直接返回 cap_reached，供编排层标记该账号本次流水线内完成。
        # 复用上面定义的选卡闸门 / _get_card / _on_invoice_* / _classify_topup 闭包。
        # ============================================================
        if invoice_daily_cap is not None:
            today_count = fetch_today_invoice_count(driver, account_id)
            if today_count is not None and today_count >= invoice_daily_cap:
                print(f"账号 {email} 当日账单数 {today_count} 已达上限 {invoice_daily_cap}，跳过 Top-up")
                return (True, "当日账单已达上限", [], '', "cap_reached",
                        {'today_count': today_count, 'generated': False, 'paid': 0, 'topup_ok': False})

            before_count = today_count if today_count is not None else 0

            # 基线余额（供 confirm 未捕获时的余额兜底判定）；同时把落地 credits 页读到的
            # 当前余额刷新入库（即便后续 Top-up 失败/跳过，DB 也已反映最新余额）
            baseline_balance = None
            try:
                reset_credit_balance(driver)
                driver.get(credits_url)
                time.sleep(4)
                dismiss_overdue_dialog(driver)
                baseline_balance = _balance_on_credits_page()
                _persist_balance(baseline_balance, "基线")
            except Exception as e:
                print(f"  读取基线余额失败: {str(e)[:80]}")

            # 打开 Top-up 弹窗并提取卡四位
            if not navigate_to_ai_credits(driver, account_id):
                return (False, "导航到充值页面或点击 Top-up 按钮失败", [], '', "failed",
                        {'today_count': before_count, 'generated': False, 'paid': 0, 'topup_ok': False})
            step_card_last4 = extract_topup_card_last4(driver)
            if not step_card_last4:
                close_topup_dialog(driver)
                return (False, "未获取到有效信用卡信息", [], '', "failed",
                        {'today_count': before_count, 'generated': False, 'paid': 0, 'topup_ok': False})

            # 1 次 Top-up 生成一张账单
            print(f"账号 {email} 当日账单数 {before_count} < {invoice_daily_cap}，发起 1 次 Top-up 生成账单...")
            pay_success, responses, step_card_last4 = fill_topup_and_confirm(driver, amount=TOPUP_AMOUNT)
            _report("topup_confirmed")
            responses = responses or []
            if not pay_success:
                # 提交步骤异常：不再直接返回，继续走下方账单处理 + 收尾。
                # generated/paid/topup_ok 由实际结果（after_count 差值 /
                # handle_unpaid_invoices / _classify_topup）如实反映。
                print(f"账号 {email} 单步 Top-up 提交异常，仍按要求继续账单支付流程")

            # 返回 credits 页，至多付掉 1 张 open invoice（复用记账回调 + 逐张换卡重试）
            post_topup_balance = None
            paid_n = 0
            cards_exhausted = False
            if not skip_invoice:
                time.sleep(5)
                reset_credit_balance(driver)
                driver.get(credits_url)
                time.sleep(5)
                check_and_handle_cf_challenge(driver)
                dismiss_overdue_dialog(driver)
                time.sleep(3)
                post_topup_balance = _balance_on_credits_page()
                # requires_action（无感 3DS）后台结算窗口，仍在卡池卡介入之前（见 _settle_topup_balance）
                post_topup_balance = _settle_topup_balance(baseline_balance, responses, post_topup_balance)
                _persist_balance(post_topup_balance, "Top-up 后")
                invoice_results = handle_unpaid_invoices(
                    driver, get_card=_get_card, on_paid=_on_invoice_paid,
                    on_failed=_on_invoice_failed, account_id=account_id,
                    should_stop=should_stop, max_invoices=1,
                    skip_invoice_check=_skip_invoice_cooldown, on_unpayable=_on_invoice_unpayable)
                paid_n = sum(1 for r in (invoice_results or []) if r.get('status') == 'paid')
                # 卡池耗尽：handle_unpaid_invoices 在无卡可用时回 status='skipped'。
                # 据此让编排层停止对该账号继续生成无法支付的账单。
                #
                # 但并发下 skipped 有两种成因，必须区分：卡池真的空了（该放弃这个
                # 账号），还是仅仅这一刻卡都被别的 worker 占着（下一轮就能拿到）。
                # 把后者当成耗尽，会让账号在本次流水线内被永久丢掉。
                cards_exhausted = any(r.get('status') == 'skipped' for r in (invoice_results or []))
                if cards_exhausted and _cards_only_contended():
                    cards_exhausted = False
                    print("  本轮无可用卡是因其它 worker 正占用，非卡池耗尽——下轮重试")
                if invoice_results:
                    print(f"单步账单处理结果: {invoice_results}")
            else:
                post_topup_balance = _record_final_balance()

            # 结束再读一次当日账单数（权威），据此让编排层判断是否达上限 / 是否有进展
            after_count = fetch_today_invoice_count(driver, account_id)
            if after_count is None:
                after_count = before_count + (1 if pay_success else 0)

            topup_ok, reason, card_fault = _classify_topup(responses, baseline_balance, post_topup_balance)

            # Top-up 因卡本身被拒 → 标记该账号对应绑定卡失效（同全量模式）
            if not topup_ok and card_fault and card_binding_model and step_card_last4:
                try:
                    for c in card_binding_model.get_by_email(email):
                        num = c.get('card_number', '')
                        if num and num.endswith(step_card_last4):
                            n = card_binding_model.mark_declined_by_number(num, reason or 'Top-up 拒付')
                            if n:
                                print(f"  已标记绑定卡 ****{step_card_last4} 为失效（{reason}）")
                            break
                except Exception as e:
                    print(f"  标记拒付卡失败: {str(e)[:80]}")

            info = {
                'today_count': after_count,
                'generated': after_count > before_count,
                'paid': paid_n,
                'topup_ok': topup_ok,
                'cards_exhausted': cards_exhausted,
            }
            print(f"账号 {email} 单步完成：当日账单 {before_count}→{after_count}，付成 {paid_n} 张，"
                  f"Top-up {'成功' if topup_ok else '未成功'}")
            return True, reason, responses or [], step_card_last4, "stepped", info

        # 读基线余额（供 Top-up 后 confirm 未捕获时的余额兜底；读不到则兜底退化为保守判失败）；
        # 同时把落地 credits 页读到的当前余额刷新入库
        baseline_balance = None
        try:
            reset_credit_balance(driver)
            driver.get(f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits")
            time.sleep(4)
            dismiss_overdue_dialog(driver)
            baseline_balance = _balance_on_credits_page()
            if baseline_balance is not None:
                print(f"  Top-up 前基线余额: ${baseline_balance:.2f}")
            _persist_balance(baseline_balance, "基线")
        except Exception as e:
            print(f"  读取基线余额失败: {str(e)[:80]}")

        print("正在跳转到 AI Credits 页面并点击充值...")
        success = navigate_to_ai_credits(driver, account_id)
        _report("navigated_to_credits")

        if not success:
            return False, "导航到充值页面或点击 Top-up 按钮失败", [], '', "failed"

        # 弹窗已打开，提取卡片后四位并检查今日是否已支付
        card_last4 = extract_topup_card_last4(driver)
        _report("extracted_card")

        if not card_last4:
            print("未能从弹窗提取到信用卡信息，无法继续充值")
            close_topup_dialog(driver)
            return False, "未获取到有效信用卡信息", [], '', "failed"

        card_already_used_today = False
        if recharge_log_model:
            card_already_used_today = recharge_log_model.has_today_record(email, card_last4)

        if card_already_used_today:
            if skip_invoice:
                print(f"卡片 ****{card_last4} 今日已有充值记录，且未选择支付卡分组，跳过")
                close_topup_dialog(driver)
                return True, "今日已充值，跳过", [], card_last4, "invoice_only"
            else:
                print(f"卡片 ****{card_last4} 今日已有充值记录，跳过 Top-up，执行账单支付流程")
                close_topup_dialog(driver)
                time.sleep(2)

                # 直接在当前 credits 页面处理 Unpaid invoices
                invoice_results = handle_unpaid_invoices(
                    driver, get_card=_get_card, on_paid=_on_invoice_paid,
                    on_failed=_on_invoice_failed, account_id=account_id,
                    should_stop=should_stop,
                    skip_invoice_check=_skip_invoice_cooldown, on_unpayable=_on_invoice_unpayable)
                if invoice_results:
                    print(f"Unpaid invoice 处理结果: {invoice_results}")
                    time.sleep(10)
                else:
                    print("未发现 Unpaid invoice")

                _record_final_balance()
                return True, "跳过充值，已处理账单", [], card_last4, "invoice_only"

        # 今日未支付过，继续充值流程
        print("正在填写充值金额并确认支付...")
        pay_success, responses, card_last4 = fill_topup_and_confirm(driver, amount=10)
        _report("topup_confirmed")
        responses = responses or []

        if not pay_success:
            # 提交步骤异常：不再直接返回。只要选了支付卡分组，仍继续进账单支付流程
            # （用户要求：提交后无论成败都去账单页）。skip_invoice=True 时无账单可处理，
            # 下方分支会跳过，最终由 _classify_topup 判失败收尾。
            print(f"账号 {email} 充值提交异常，仍按要求继续账单支付流程")
        else:
            print(f"账号 {email} 充值 $10 已提交")

        post_topup_balance = None
        if not skip_invoice:
            # 有支付卡分组，等待页面更新后检查 Unpaid invoices
            time.sleep(5)
            print("正在返回 Credits 页面读取余额，随后处理 Unpaid invoices...")
            credits_url = f"https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits"
            reset_credit_balance(driver)
            driver.get(credits_url)
            time.sleep(5)
            check_and_handle_cf_challenge(driver)
            dismiss_overdue_dialog(driver)
            time.sleep(3)
            # 处理任何账单之前先读一次余额——只反映 Top-up 效果，避免账单支付垫高余额混淆兜底判定
            post_topup_balance = _balance_on_credits_page()
            # confirm=requires_action（无感 3DS）时，扣款可能在此刻后才结算——给余额一个短暂窗口，
            # 仍在卡池卡介入之前，保证 requires_action 的 topup 能凭余额增长被正确判为成功
            post_topup_balance = _settle_topup_balance(baseline_balance, responses, post_topup_balance)
            _persist_balance(post_topup_balance, "Top-up 后")

            invoice_results = handle_unpaid_invoices(
                driver, get_card=_get_card, on_paid=_on_invoice_paid,
                on_failed=_on_invoice_failed, account_id=account_id,
                should_stop=should_stop,
                skip_invoice_check=_skip_invoice_cooldown, on_unpayable=_on_invoice_unpayable)
            if invoice_results:
                print(f"Unpaid invoice 处理结果: {invoice_results}")
                time.sleep(10)
            _record_final_balance()
        else:
            print("未选择支付卡分组，跳过 Unpaid invoice 处理")
            # 无账单支付，最终余额即 Top-up 后余额，可直接用于兜底
            post_topup_balance = _record_final_balance()

        # === 判定 Top-up 真实结果：Stripe confirm 为权威，confirm 未捕获时用余额兜底 ===
        topup_ok, reason, card_fault = _classify_topup(responses, baseline_balance, post_topup_balance)
        if topup_ok:
            return True, "", responses or [], card_last4, "topup"

        # Top-up 未成功：若归因于卡本身（拒付/过期/需3DS），标记该账号对应绑定卡失效
        if card_fault and card_binding_model and card_last4:
            try:
                for c in card_binding_model.get_by_email(email):
                    num = c.get('card_number', '')
                    if num and num.endswith(card_last4):
                        n = card_binding_model.mark_declined_by_number(num, reason or 'Top-up 拒付')
                        if n:
                            print(f"  已标记绑定卡 ****{card_last4} 为失效（{reason}）")
                        break
            except Exception as e:
                print(f"  标记拒付卡失败: {str(e)[:80]}")

        print(f"账号 {email} Top-up 未成功: {reason}")
        return False, reason, responses or [], card_last4, "failed"

    except Exception as e:
        print(f"充值过程异常: {e}")
        if invoice_daily_cap is not None:
            return False, str(e), [], '', "failed", {'today_count': None}
        return False, str(e), [], '', "failed"

    finally:
        # 兜底释放。正常路径下每笔支付有结果就已经 _release_card 了，这里只清理
        # 异常路径漏放的（选中卡之后、支付回调之前抛异常）。不兜底的话那张卡会
        # 一直挂在内存登记里，其它 worker 永远拿不到。
        if payment_registry is not None:
            for _num in list(_claimed_numbers):
                payment_registry.release(_num)
        if driver:
            print("正在关闭浏览器...")
            close_driver(driver)
