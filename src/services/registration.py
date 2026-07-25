"""
注册 & 绑卡 / 充值核心业务逻辑 —— 站点流程存根（占位）。

项目名为 openrouter-auto-task，**目标自动化站点为 https://opencode.ai**。
本模块原为 Cloudflare 站点的注册/绑卡/充值编排。改造后 Cloudflare 专属的页面流程
（注册页、Turnstile、Stripe 绑卡、AI Gateway 充值等）已从编排层剥离。以下 4 个公共
函数保留原有签名与返回契约说明，供 app.py / routes.py 的上层编排与并发调度继续
import 与调用；函数体统一抛 NotImplementedError，等待按 opencode.ai 实际流程逐个接入。

接入时对照 design.md / prd.md 的站点耦合面，把 driver.py 中标记为
`LEGACY Cloudflare-specific` 的浏览器方法替换为 opencode.ai 版实现，再在此填充编排。
原 Cloudflare 实现保留在本文件的 git 历史中，可作为接入参考。
"""

_NOT_IMPLEMENTED = (
    "opencode.ai 站点流程待接入：{name}。当前为框架存根，尚未实现 opencode.ai 的"
    "注册/绑卡/充值页面自动化。"
)


def register_one_account(db, account_model, card_info_list=None, login_password=None,
                         monitor_callback=None, max_bindable_cards=2, captcha_api_key=None):
    """注册单个账号并添加信用卡。

    原返回契约: (邮箱, 密码, 是否成功)
    """
    raise NotImplementedError(_NOT_IMPLEMENTED.format(name="register_one_account"))


def register_and_bind_cards(db, account_model, card_binding_model, task_id,
                            batch_records, login_password=None, max_bindable_cards=2,
                            captcha_api_key=None, monitor_callback=None,
                            claim_more=None, card_pool_model=None):
    """注册一个账号并逐张绑定信用卡。

    原返回契约: (email, password, bound_count)
    """
    raise NotImplementedError(_NOT_IMPLEMENTED.format(name="register_and_bind_cards"))


def bind_cards_to_existing_account(account_model, card_binding_model, task_id,
                                   email, login_password, batch_records,
                                   max_bindable_cards=2, captcha_api_key=None,
                                   monitor_callback=None, claim_more=None,
                                   card_pool_model=None):
    """登录已有账号并补绑信用卡。

    原返回契约: (bound_count, login_ok)
    """
    raise NotImplementedError(_NOT_IMPLEMENTED.format(name="bind_cards_to_existing_account"))


def recharge_account(email, login_password, recharge_log_model=None, monitor_callback=None,
                     skip_invoice=False, payment_cards=None,
                     valid_card_model=None, card_pool_model=None, account_model=None,
                     should_stop=None, card_binding_model=None, card_state_model=None,
                     payment_registry=None):
    """登录 opencode 账号并在 zen 控制台 Stripe Checkout 充值（美元，$20 credits）。

    编排：create_driver(profile_id=email) → 确保 opencode 登录 → 从 payment_cards 逐张
    尝试 Stripe 付款，付成一张即返回（该账号本次访问消耗到 1 张成功卡）。沿途把明确拒付
    的卡标为 invalid、逐卡写 recharge_logs（成功/失败+原因）。3DS 记冷却、hCaptcha 停手
    —— 二者均不算「消耗」、不写卡消耗日志。浏览器操作见 browser/opencode_billing。

    返回契约: (ok, err, responses, card_last4, outcome)，outcome ∈ {"topup"(成功), "failed"}。

    卡消耗与逐卡记账集中在本函数：成功→card_pool 标 paid + valid_card + recharge_logs
    success；明确拒付→card_pool 标 invalid + recharge_logs failed（带原因）。调用方无需
    再预建占位 log。payment_registry 传入时对每张卡做 in-flight 排他（并发安全网）。
    """
    from src.browser.driver import create_driver, close_driver
    from src.browser import opencode_billing as ob

    responses = []

    def _log_card_attempt(card, ok, reason, result):
        """逐卡写一条 recharge_logs（成功/失败）。记账集中于此，避免上层重复。"""
        if not recharge_log_model:
            return
        try:
            log_id = recharge_log_model.create(email, card.get("number", ""), amount=20)
            if ok:
                recharge_log_model.mark_success(log_id, api_response={"result": result})
            else:
                recharge_log_model.mark_failed(log_id, error=(reason or "")[:200],
                                               api_response={"result": result})
        except Exception:
            pass

    # 可用支付卡（按序逐张尝试，某张失败/触发 3DS 即换下一张）
    if not payment_cards:
        return (False, "无可用支付卡（card_pool 为空或该分组无可用卡）", responses, "", "failed")

    # 过滤掉处于临时冷却期的卡（3DS 或「曾成功卡本次被拒」的速率冷却）。上层选卡通常已预过滤，
    # 这里是安全网。
    cards = payment_cards
    if card_state_model:
        try:
            cards = [c for c in payment_cards
                     if not card_state_model.in_cooldown(c.get("number", ""))]
        except Exception:
            cards = payment_cards
        if not cards:
            return (False, "所有支付卡均处于临时冷却期，暂无可用卡", responses, "", "failed")

    last4 = str(cards[0].get("number", ""))[-4:]

    session = None
    try:
        session = create_driver(headless=False, profile_id=email)
        if monitor_callback:
            monitor_callback(session, f"为 {email} 启动浏览器")

        wid, detail = ob.ensure_opencode_session(session, monitor_callback, login_password, email)
        if not wid:
            return (False, f"opencode 未登录：{detail}", responses, last4, "failed")

        # 单次充值最多尝试的卡数上限：卡池可能上千张，若不设限，一批坏卡会在同一
        # workspace 上连续制造大量拒付，极易触发 Stripe/opencode 的反欺诈 velocity
        # 风控（拒付率过高 → 临时封锁 workspace 或要求人工验证）。达到上限即停手，
        # 保护账号可用性，剩余卡留待下次。可用环境变量 OPENCODE_RECHARGE_MAX_ATTEMPTS 调。
        import os
        try:
            max_attempts = int(os.environ.get("OPENCODE_RECHARGE_MAX_ATTEMPTS", "8"))
        except ValueError:
            max_attempts = 8
        max_attempts = max(1, max_attempts)

        errs = []
        attempts = 0
        for idx, card in enumerate(cards):
            if should_stop and should_stop():
                raise InterruptedError("用户请求停止")
            if attempts >= max_attempts:
                errs.append(f"已达单次最多尝试 {max_attempts} 张卡上限，"
                            f"停止以避免触发风控（剩余 {len(cards) - idx} 张未试）")
                if monitor_callback:
                    monitor_callback(session, errs[-1])
                break

            num = card.get("number", "")
            # 卡排他（并发安全网）：被其它 worker 占用则跳过，不计入尝试次数
            if payment_registry is not None and not payment_registry.try_acquire(num, email):
                continue
            try:
                attempts += 1
                card_last4 = str(num)[-4:]
                last4 = card_last4

                result = ob.recharge_via_stripe(session, card, wid,
                                                monitor=monitor_callback, should_stop=should_stop)
                responses.append({"card_last4": card_last4, **result})

                if result["ok"]:
                    # 支付成功：标 paid + 记有效卡 + 账号状态 + 逐卡记账。
                    # 注意：paid 卡「不」永久消耗——paid 不在 NOT_SELECTABLE 内，后续仍可复选
                    # 复用；仅当这张曾成功的卡再次被拒时才进入 24h 速率冷却（见下方 else 分支）。
                    if card_pool_model:
                        try:
                            card_pool_model.mark_status_by_number(num, "paid")
                        except Exception:
                            pass
                    if valid_card_model:
                        try:
                            valid_card_model.record(card, source_type="payment", source_email=email)
                        except Exception:
                            pass
                    if account_model:
                        try:
                            account_model.update_status(email, "recharged")
                        except Exception:
                            pass
                    _log_card_attempt(card, True, "", result)
                    return (True, "", responses, card_last4, "topup")

                outcome = result.get("outcome")
                reason = f"卡{card_last4}: {outcome} - {result.get('err','')}"
                errs.append(reason)

                if outcome == "needs_captcha":
                    # hCaptcha 是账号/风控级拦截，换卡无用且会持续触发风控——立即停手。
                    # 不标卡无效、不写卡消耗日志，保留其余卡交人工过验证码后重试。
                    if monitor_callback:
                        monitor_callback(session, f"{reason}；需人工过 hCaptcha，停止换卡")
                    return (False, "hCaptcha 人机验证拦截，需人工完成后重试：" + reason,
                            responses, card_last4, "failed")
                elif outcome == "error":
                    # 页面/基础设施故障（未找到入口/选卡失败/填卡失败/点 Pay 失败），非卡问题：
                    # 不判无效、不冷却、不记账、不消耗——留着这张卡下次重试，避免因页面故障误烧卡。
                    if monitor_callback:
                        monitor_callback(session, f"{reason}；页面/基础设施异常，跳过不消耗此卡")
                elif outcome == "unknown":
                    # 已点 Pay 提交，但超时内未确认到账，也无明确拒付/3DS/captcha 信号。不确定
                    # 是否卡的问题，保守处理：记一条失败日志留痕，但不改卡状态、不消耗，留待重试。
                    _log_card_attempt(card, False, reason, result)
                else:
                    # failed（明确拒付 / 3DS 交互挑战 / 3DS 认证失败）：按用户规则——
                    #   曾成功过的卡 → 24h 速率冷却，不判无效（可复用）；
                    #   从未成功过的卡 → 判无效（坏卡，永久剔除）。
                    prior_success = False
                    if recharge_log_model:
                        try:
                            prior_success = recharge_log_model.last_success_at(num) is not None
                        except Exception:
                            prior_success = False
                    if prior_success:
                        if card_state_model:
                            try:
                                card_state_model.set_cooldown(
                                    num, hours=24, reason="曾成功卡本次支付失败，速率冷却")
                            except Exception:
                                pass
                    elif card_pool_model:
                        try:
                            card_pool_model.mark_invalid_by_number(num)
                        except Exception:
                            pass
                    _log_card_attempt(card, False, reason, result)

                if monitor_callback:
                    monitor_callback(session, f"{reason}；尝试下一张卡（{idx+1}/{len(cards)}）")
            finally:
                if payment_registry is not None:
                    payment_registry.release(num)

        return (False, "所有支付卡均未成功：" + " | ".join(errs), responses, last4, "failed")

    except InterruptedError:
        raise
    except Exception as e:
        return (False, str(e), responses, last4, "failed")
    finally:
        if session:
            try:
                close_driver(session)
            except Exception:
                pass
