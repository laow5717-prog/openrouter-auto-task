"""充值编排 —— **平台无关**。

recharge_account 是本模块唯一活着的函数，也是整个抽象改造的收益点：余额预检 →
逐卡试付 → 按 outcome 分派 → 卡状态与记账，这套骨架不含任何站点知识。平台特有的
动作（登录、读余额、点付款、判结果）全部经 PlatformAdapter 转接，接第二个平台
不需要改这里一行。

另外三个函数（register_one_account / register_and_bind_cards /
bind_cards_to_existing_account）是 Cloudflare 时代绑卡编排的存根，保留签名供上层
import；对应的浏览器实现已在前置清理里删除，要接新流程时按 PlatformAdapter 重写。
"""

_NOT_IMPLEMENTED = (
    "站点流程待接入：{name}。当前为框架存根——原 Cloudflare 绑卡编排已随浏览器层"
    "一并删除，新流程应按 PlatformAdapter 接口实现。"
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
                     payment_registry=None, captcha_api_key=None,
                     captcha_server="api.multibot.cloud", proxy=None,
                     browser_factory=None, verify_link=None,
                     platform='opencode', platform_account_model=None, adapter=None):
    """登录目标平台并逐卡尝试付款充值。

    **本函数是平台无关的编排骨架**：余额预检 → 逐卡试付 → 按 outcome 分派 → 卡状态与
    记账。平台特有的动作（怎么登录、余额在哪读、付款怎么点、结果怎么判）全部经
    adapter 转接，本函数不知道也不需要知道目标是哪个站点。

    编排：建浏览器会话（原生 Playwright 栈，hCaptcha token 注入仅在原生栈生效；与
    create_driver 复用同一 profile 目录，登录态不丢）→ 装 hCaptcha hook →
    adapter.ensure_session → 读实时余额，≥ adapter.recharge_skip_balance 则跳过充值并
    归档 → 否则从 payment_cards 逐张 adapter.top_up，付成一张即返回。
    沿途把明确拒付的卡标为 invalid、逐卡写 recharge_logs（成功/失败+原因）。

    captcha_api_key: 传入则 init_solver(key, server=captcha_server) 并装 hook 自动解 hCaptcha；
                     不传则退化为旧行为（检测到 hCaptcha 提示人工、超时 needs_captcha）。
    captcha_server:  求解服务域名，默认 Multibot（api.multibot.cloud）；可传 '2captcha.com'。
    browser_factory: 可选 callable(email) -> BrowserSession，替换默认的本地 Chrome 启动
                     （AdsPower 指纹浏览器接入走这里）。为 None 时行为与接入前逐字一致。
                     有 factory 时 proxy 参数被忽略——代理由 factory 那一侧绑定到环境上。
    verify_link:     该账号的若安收信链接（accounts.email_verify_link）。GitHub 新设备
                     邮箱验证用它自动收码回填；不传则退回等人工。指纹浏览器下每个账号
                     都是全新环境，新设备验证几乎必然触发，缺它会让流水线停下来等人。

    adapter:         PlatformAdapter；省略时按 platform 从注册表解析。
    platform / platform_account_model: 目标平台 slug 与平台账号模型。归档、充值成功、
                     余额落库都写到 platform_accounts 的 (platform, email) 那一行，
                     所以同一邮箱在别的平台的进度不受影响。GitHub 被 flag 是例外——
                     那是身份层的封禁，写 accounts.identity_status，对所有平台生效。

    返回契约: (ok, err, responses, card_last4, outcome)，
    outcome ∈ {"topup"(成功), "failed", "archived"(余额≥阈值已归档、未扣款),
               "flagged"(GitHub 账号被 flag 无法授权 OAuth，已标身份层 flagged)}。

    卡消耗与逐卡记账集中在本函数：成功→card_pool 标 paid + valid_card + recharge_logs
    success；明确拒付→card_pool 标 invalid + recharge_logs failed（带原因）。调用方无需
    再预建占位 log。payment_registry 传入时对每张卡做 in-flight 排他（并发安全网）。
    """
    from src.browser.driver import create_driver_vanilla, close_driver
    from src.services import captcha as captcha_solver
    import src.platforms as platforms
    from src.platforms.base import Credentials

    if adapter is None:
        adapter = platforms.get(platform)

    # 余额跳过阈值（美元）：登录后实时余额 ≥ 此值即跳过充值并归档账号。各平台不同。
    skip_balance = adapter.recharge_skip_balance

    responses = []

    def _log_card_attempt(card, ok, reason, result):
        """逐卡写一条 recharge_logs（成功/失败）。记账集中于此，避免上层重复。"""
        if not recharge_log_model:
            return
        try:
            log_id = recharge_log_model.create(platform, email, card.get("number", ""), amount=20)
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
                     if not card_state_model.in_cooldown(platform, c.get("number", ""))]
        except Exception:
            cards = payment_cards
        if not cards:
            return (False, "所有支付卡均处于临时冷却期，暂无可用卡", responses, "", "failed")

    last4 = str(cards[0].get("number", ""))[-4:]

    session = None
    try:
        # 原生 Playwright 栈：hCaptcha token 注入只在原生栈生效（Patchright 阉割了 add_init_script）；
        # 与 create_driver 复用同一 profile 目录（data/profiles/<email>），登录态照常复用。
        # browser_factory 给出时改由它建会话（AdsPower 环境经 CDP 接管，同样是原生栈，
        # 已实测 add_init_script 前置注入照常生效）。
        session = (browser_factory(email) if browser_factory is not None
                   else create_driver_vanilla(profile_id=email, proxy=proxy))
        if monitor_callback:
            monitor_callback(session, f"为 {email} 启动浏览器")

        # 装 hCaptcha hook（须在导航到含 hCaptcha 的 Stripe 结账页之前）。未配 captcha_api_key
        # 时不装、不解，充值行为与改造前一致（检测到 hCaptcha 提示人工、超时 needs_captcha）。
        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key, server=captcha_server)
        if captcha_solver.is_available():
            captcha_solver.install_hcaptcha_hook(session)

        sess = adapter.ensure_session(
            session,
            Credentials(email=email, login_password=login_password, verify_link=verify_link),
            monitor=monitor_callback,
        )
        wid, detail = sess.tenant_id, sess.detail
        if not sess.ok:
            if sess.blocked_by_identity:
                # 身份供给侧被封（opencode 的场景是 GitHub 反滥用 flag，无法授权任何
                # 第三方 OAuth）——这是跨平台通用的终态：标记后由上层退出每日轮转，
                # 不再每轮空开浏览器。
                if account_model:
                    try:
                        account_model.update_identity_status(email, "flagged")
                    except Exception:
                        pass
                return (False, f"{platform} 未登录：{detail}", responses, last4, "flagged")
            return (False, f"{platform} 未登录：{detail}", responses, last4, "failed")

        # R2 归档预检：登录后读实时余额，≥ 阈值即跳过充值并归档（不试任何卡、不扣款）。
        # 以实时余额为准——DB 余额会随 credits 消耗过时，不可作归档依据。
        try:
            cur_bal = adapter.read_balance(session, wid, monitor_callback)
        except Exception:
            cur_bal = None
        if cur_bal is not None and cur_bal >= skip_balance:
            if platform_account_model:
                try:
                    platform_account_model.update_status(platform, email, "archived")
                    platform_account_model.update_balance(platform, email, cur_bal)
                    platform_account_model.update_tenant_id(platform, email, wid)
                except Exception:
                    pass
            if monitor_callback:
                monitor_callback(session, f"{email} 余额 ${cur_bal} ≥ ${skip_balance}，跳过充值并归档")
            return (False, f"余额 ${cur_bal} ≥ ${skip_balance}，跳过并归档",
                    responses, last4, "archived")

        # 单次充值最多尝试的卡数上限：卡池可能上千张，若不设限，一批坏卡会在同一
        # 租户上连续制造大量拒付，极易触发支付方的反欺诈 velocity 风控（拒付率过高
        # → 临时封锁租户或要求人工验证）。达到上限即停手，保护账号可用性，剩余卡留待
        # 下次。阈值由各平台自己定——风控松紧本就因平台而异。
        max_attempts = adapter.max_card_attempts

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
            if payment_registry is not None and not payment_registry.try_acquire(platform, num, email):
                continue
            try:
                attempts += 1
                card_last4 = str(num)[-4:]
                last4 = card_last4

                pay = adapter.top_up(session, wid, card,
                                     monitor=monitor_callback, should_stop=should_stop)
                result = vars(pay)
                responses.append({"card_last4": card_last4, **result})

                if pay.ok:
                    # 支付成功：标 paid + 记有效卡 + 账号状态 + 逐卡记账。
                    # 注意：paid 卡「不」永久消耗——paid 不在 NOT_SELECTABLE 内，后续仍可复选
                    # 复用；仅当这张曾成功的卡再次被拒时才进入 24h 速率冷却（见下方 else 分支）。
                    if card_pool_model:
                        try:
                            card_pool_model.mark_status_by_number(platform, num, "paid")
                        except Exception:
                            pass
                    if valid_card_model:
                        try:
                            valid_card_model.record(platform, card, source_type="payment",
                                                    source_email=email)
                        except Exception:
                            pass
                    if platform_account_model:
                        try:
                            platform_account_model.update_status(platform, email, "recharged")
                            # 充值到账后把新余额写回 DB（result.balance_after 来自
                            # detect_payment_result 读到的 Current Balance）。此前只更状态不更余额，
                            # 导致列表页余额一直是旧值。None 时 update_balance 内部会安全跳过。
                            platform_account_model.update_balance(
                                platform, email, result.get("balance_after"))
                            platform_account_model.update_tenant_id(platform, email, wid)
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
                    # 「曾成功过」按**本平台**算：在别的平台成功过不能豁免这里的判废，
                    # 否则跨平台复用的坏卡在新平台永远只进冷却、每轮被重选，白耗额度。
                    prior_success = False
                    if recharge_log_model:
                        try:
                            prior_success = recharge_log_model.last_success_at(platform, num) is not None
                        except Exception:
                            prior_success = False
                    if prior_success:
                        if card_state_model:
                            try:
                                card_state_model.set_cooldown(
                                    platform, num, hours=24,
                                    reason="曾成功卡本次支付失败，速率冷却")
                            except Exception:
                                pass
                    elif card_pool_model:
                        try:
                            card_pool_model.mark_invalid_by_number(platform, num)
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
