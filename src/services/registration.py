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


def ensure_apikey(adapter, platform_account_model, platform, email, session, wid,
                  monitor=None, only_if_missing=False, why=''):
    """登录态下抓平台 API key 落库。best-effort，返回是否落库成功。

    两个调用时机，语义不同：
      - only_if_missing=True —— **任务跑到这个账号、刚登录成功时**的补漏。库里已经
        有 key 就直接返回，连页面都不导航，所以对绝大多数账号是零开销。之所以要有
        这一道，是因为「充值成功后顺手抓」只覆盖得到当次成功充值的账号：一个早就
        充过、这次全程拒付的账号，key 要是当初没抓着，就再也没有机会补上了。
      - only_if_missing=False —— 充值成功 / 余额达标归档后的常规抓取，覆盖写。

    ⚠️ best-effort **不等于**静默。这里原来是一个光秃秃的 `except Exception: pass`，
    连「抓到了没有」都不说一声，于是 2026-08-16 出现「账号充值成功、apikey 列却是空」
    时，日志里一个字都没有——分不清是没执行、导航失败、页面没渲染出来，还是写库失败。
    现在每种结局各记一条。日志走 print（已被 AppState._patch_prints 劫持进平台日志流），
    不占 monitor 的截图通道。
    """
    if not (platform_account_model and wid):
        print(f"  ⚠️ {email} 跳过抓 API key（缺 tenant_id 或未接账号模型）")
        return False
    fetch = getattr(adapter, "fetch_apikey", None)
    if not callable(fetch):
        return False        # 平台没这个能力，属于正常，不值一条日志
    # 已知拿不到明文的平台（infron：key 页只显示脱敏串）也不值一条日志——否则
    # 下面那句「没抓到」会在它每一笔成功充值后重复出现，把真正的异常淹掉。
    if not getattr(adapter, "apikey_fetchable", True):
        return False
    if only_if_missing:
        try:
            row = platform_account_model.get(platform, email) or {}
        except Exception:
            row = {}
        if (row.get('apikey') or '').strip():
            return False    # 已经有了，不导航、不打日志
        print(f"  ℹ️ {email} 库里没有 API key，{why or '本次任务'}顺手补抓一次")
    try:
        key = fetch(session, wid, monitor)
    except Exception as e:
        print(f"  ⚠️ {email} 抓 API key 出错（不影响主流程）: {type(e).__name__}: {str(e)[:150]}")
        return False
    if not key:
        print(f"  ⚠️ {email} 没抓到 API key：keys 页里找不到 sk- 明文，可稍后人工补抓"
              f"（scripts/fetch_apikeys.py）")
        return False
    try:
        platform_account_model.update_apikey(platform, email, key)
    except Exception as e:
        print(f"  ⚠️ {email} API key 落库失败: {str(e)[:150]}")
        return False
    print(f"  ✅ {email} 已抓取并落库 API key（{key[:8]}…{key[-4:]}）")
    if monitor:
        monitor(session, f"{email} 已抓取并落库 API key")
    return True


def recharge_account(email, login_password, recharge_log_model=None, monitor_callback=None,
                     skip_invoice=False, payment_cards=None,
                     valid_card_model=None, card_pool_model=None, account_model=None,
                     should_stop=None, card_binding_model=None, card_state_model=None,
                     payment_registry=None, captcha_api_key=None,
                     captcha_server="api.multibot.cloud", proxy=None,
                     browser_factory=None, verify_link=None,
                     platform='opencode', platform_account_model=None, adapter=None,
                     recharge_cfg=None):
    """登录目标平台并逐卡尝试付款充值，**一个账号连续充多笔**直到充不动为止。

    **本函数是平台无关的编排骨架**：余额预检 → 逐卡试付 → 按 outcome 分派 → 卡状态与
    记账。平台特有的动作（怎么登录、余额在哪读、付款怎么点、结果怎么判）全部经
    adapter 转接，本函数不知道也不需要知道目标是哪个站点。

    编排：建浏览器会话（原生 Playwright 栈，hCaptcha token 注入仅在原生栈生效；与
    create_driver 复用同一 profile 目录，登录态不丢）→ 装 hCaptcha hook →
    adapter.ensure_session → 读实时余额，≥ recharge_cfg.balance_cap 则跳过充值并
    归档 → 否则从 payment_cards 逐张 adapter.top_up。

    ## 一次成功后为什么不返回，以及为什么不换卡

    早先的实现是「付成一张卡即 return」，一个账号一轮只充一笔。现在成功后继续充，直到
    下列任一条件成立才收手换账号：

      - 达到 adapter.max_card_attempts（试卡上限，防发卡行 velocity 风控；**0 = 不限制**，
        opencode 现在就是 0）
      - 余额达到 recharge_cfg.balance_cap（单账号余额上限）
      - 遇 needs_captcha（账号级拦截，立即停手）
      - payment_cards 用尽
      - 用户停止

    **续充用的是同一张卡**（粘卡）。中间那版是「成功后换下一张卡充」，代价是能过款的好卡
    被闲置、每一笔都拿一张没验证过的新卡去赌——拒付率和账号风控压力都由此推高。现在一张卡
    过款之后就一直用它，直到它自己失败（拒付 / 页面故障 / 未定案）才换下一张。

    粘卡把「卡用完了」这个天然刹车拿掉了，所以上面那份清单里 max_card_attempts 与
    balance_cap 成了仅有的两道上限；两者都在内层循环里逐笔复查，粘卡绕不过去。

    ⚠️ max_card_attempts 设成 0（不限制）时，那两道上限**只剩 balance_cap 一道**。
    对粘住的好卡没有影响——它照样充到 balance_cap 就换账号；影响的是坏卡路径：
    一直换卡试下去，直到卡池耗尽或遇 hCaptcha，单账号单次的拒付次数不再有上限。

    归档预检与循环上限现在是**同一个数** recharge_cfg.balance_cap。早先它们是两个：
    归档用 adapter.recharge_skip_balance（20）、循环用 balance_cap（200），于是一个
    充过一笔的账号下次来必被归档——「一个账号只能充一笔」的旧行为从后门溜回来了。

    ## 卡的判废与冷却

    每次明确拒付：无条件进 recharge_cfg.fail_cooldown_hours 冷却 + 连续失败计数 +1；
    计数达到 recharge_cfg.fail_threshold() 才判 invalid。成功一次即计数清零，且成功的卡
    **不冷却**（否则同账号连充无从谈起）。好卡的豁免不再靠这里查 last_success_at，而是
    靠 mark_invalid_by_number 底层那道 valid_cards 守卫——它本就是所有「标无效」入口的
    最终收口，现在成了唯一收口。

    逐卡写 recharge_logs（成功/失败+原因+该笔实际金额）。

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
    recharge_cfg:    RechargeConfig（金额区间 / 余额上限 / 判废阈值 / 冷却时长）。
                     省略时用 cfg.recharge。UI 传来的覆盖值由 API 层构造成新实例，
                     绝不原地改全局单例——两个平台并发时那会互相覆盖。
    platform / platform_account_model: 目标平台 slug 与平台账号模型。归档、充值成功、
                     余额落库都写到 platform_accounts 的 (platform, email) 那一行，
                     所以同一邮箱在别的平台的进度不受影响。GitHub 被 flag 是例外——
                     那是身份层的封禁，写 accounts.identity_status，对所有平台生效。

    返回契约: (ok, err, responses, card_last4, outcome)，
    outcome ∈ {"topup"(成功), "failed", "archived"(余额≥阈值已归档、未扣款),
               "flagged"(GitHub 账号被 flag 无法授权 OAuth，已标身份层 flagged)}。

    卡消耗与逐卡记账集中在本函数：成功→card_pool 标 paid + valid_card + recharge_logs
    success；连续拒付达阈值→card_pool 标 invalid + recharge_logs failed（带原因）。
    调用方无需再预建占位 log。payment_registry 传入时对每张卡做 in-flight 排他
    （并发安全网）。成功笔数可由调用方从 responses 里数 ok=True 的条目得到。
    """
    from src.browser.driver import create_driver_vanilla, close_driver
    from src.services import captcha as captcha_solver
    from src.config import cfg
    import src.platforms as platforms
    from src.platforms.base import Credentials

    if adapter is None:
        adapter = platforms.get(platform)
    if recharge_cfg is None:
        recharge_cfg = cfg.recharge

    # 归档阈值（美元）：登录后实时余额 ≥ 此值即跳过充值并归档账号。只在登录后判一次。
    #
    # 用 balance_cap 而**不是** adapter.recharge_skip_balance。两者本是同一件事的两个
    # 数字，而且在打架：skip_balance 两平台都是 20，balance_cap 默认 200——一个账号
    # 成功充过一笔后余额必然 ≥20，下次再来就被归档，哪怕它离 200 还差得远。
    # 结果是「一个账号只能充一笔」的旧行为从后门回来了，而且是以「已归档」的面目出现，
    # 从日志上看像是账号真的满了。
    #
    # skip_balance 是「一个账号充 $20 就算完事」那个时代的产物，正是本次改造要取代的。
    # 现在只保留 balance_cap 一个数：它同时是连充循环的上限和归档的判据，语义一致。
    # adapter.recharge_skip_balance 保留但不再被编排层使用（见 platforms/base.py）。
    skip_balance = recharge_cfg.balance_cap

    responses = []

    def _grab_apikey(sess, wid, only_if_missing=False, why=''):
        """模块级 ensure_apikey 的薄封装，把闭包里的 adapter/platform/email 补齐。"""
        return ensure_apikey(adapter, platform_account_model, platform, email,
                             session, wid, monitor=monitor_callback,
                             only_if_missing=only_if_missing, why=why)

    def _log_card_attempt(card, ok, reason, result, amount):
        """逐卡写一条 recharge_logs（成功/失败），amount 是**这一笔的实际金额**。

        此前这里写死 20，于是金额随机化后账面全是 20、对不上真实扣款。
        """
        if not recharge_log_model:
            return
        try:
            log_id = recharge_log_model.create(platform, email, card.get("number", ""),
                                               amount=amount)
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

    # 过滤掉处于临时冷却期的卡（3DS，或上一次充值被拒后的 fail_cooldown_hours 冷却）。
    # 上层选卡通常已预过滤，这里是安全网——「同一张卡两次使用间隔 ≥24h」最终靠它兜住。
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

        # 登录成功即补抓 API key —— 但只在库里那一格是空的时候。
        #
        # 「充值成功后顺手抓」漏得到的正是最需要补的那批：一个早就充过的账号，这次
        # 全程拒付（卡池质量差时这是常态），一次都不会走到成功分支，key 当初没抓着
        # 就永远补不上。而此刻会话必在登录态、wid 也拿到了，抓一次的边际成本只有
        # 一次页面导航，且**只对缺 key 的账号付这个成本**。
        _grab_apikey(session, wid, only_if_missing=True, why='充值任务')

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
            _grab_apikey(session, wid)
            if monitor_callback:
                monitor_callback(session, f"{email} 余额 ${cur_bal} ≥ ${skip_balance}，跳过充值并归档")
            return (False, f"余额 ${cur_bal} ≥ ${skip_balance}，跳过并归档",
                    responses, last4, "archived")

        # 单次充值最多尝试的卡数上限：卡池可能上千张，若不设限，一批坏卡会在同一
        # 租户上连续制造大量拒付，可能触发支付方的反欺诈 velocity 风控（拒付率过高
        # → 临时封锁租户或要求人工验证）。达到上限即停手，保护账号可用性，剩余卡留待
        # 下次。阈值由各平台自己定——风控松紧本就因平台而异。
        #
        # **0 或负数 = 不限制**（opencode 当前如此），此时这道闸完全让开，收手只靠
        # 余额达 balance_cap / 卡池耗尽 / hCaptcha / 用户停止这几条。
        max_attempts = adapter.max_card_attempts

        errs = []
        attempts = 0
        # ── 连充状态 ──
        # paid_count    本次会话成功的笔数。非零即返回 topup，哪怕后面被风控打断。
        # session_topped 本次会话累计充了多少钱。它是 balance_cap 的**兜底判据**：
        #               PaymentResult.balance_after 是 Optional，适配器读不到就是 None
        #               （infron 很常见），只看余额的话循环会一直跑到 max_attempts。
        # stop_note     跳出原因，写进返回的 err 供上层记日志。
        paid_count = 0
        session_topped = 0.0
        last_paid4 = ''
        stop_note = ''
        stop_err = ''      # 非空时作为「一笔都没成」情况下返回的 err，覆盖 errs 汇总
        # 粘卡内层循环的「穿透」标志。内层跑在 try 里，直接 break 只跳出内层；置位后由
        # finally 放开卡、再由外层判定收手。用标志而不是异常，是因为这条路径是正常收尾。
        stop_all = False
        for idx, card in enumerate(cards):
            if should_stop and should_stop():
                raise InterruptedError("用户请求停止")
            if max_attempts > 0 and attempts >= max_attempts:
                errs.append(f"已达单次最多尝试 {max_attempts} 张卡上限，"
                            f"停止以避免触发风控（剩余 {len(cards) - idx} 张未试）")
                stop_note = errs[-1]
                if monitor_callback:
                    monitor_callback(session, errs[-1])
                break

            num = card.get("number", "")
            # 卡排他（并发安全网）：被其它 worker 占用则跳过，不计入尝试次数
            if payment_registry is not None and not payment_registry.try_acquire(platform, num, email):
                continue
            try:
                # 冷却实时复查。上面那份 cards 是**会话开始时**的快照，而一次会话要连充
                # 多笔、可能跑很久；期间别的 worker 完全可能把这张卡刷失败并设上冷却，
                # 快照里它还在，照刷就违反了「当日失败不再用」。
                # in_flight 挡不住（对方早已释放），_used 只覆盖同一轮，所以按 DB 再问一次。
                # 必须写在 try 之内：写在 try_acquire 与 try 之间的话，continue 会跳过
                # finally 里的 release，这张卡的 in-flight 占用就永久泄漏了。
                # 异常一律放行——安全网不该比它保护的流程更脆弱。
                if card_state_model is not None:
                    try:
                        if card_state_model.in_cooldown(platform, num):
                            continue          # 不计入 attempts：跳过不算一次尝试
                    except Exception:
                        pass
                # ── 粘卡内层循环 ──
                # 「一张卡刷成一笔就换下一张」曾是这里的行为，代价是好卡被闲置、坏卡被
                # 反复拿去试：卡池里能过款的卡本就稀有，一笔一换等于每笔都拿新卡赌运气，
                # 拒付率和账号风控压力都由此推高。现在改成**成功就粘住**：这张卡一直用到
                # 它自己失败（或撞上试卡上限 / 余额上限 / hCaptcha / 用户停止）为止。
                #
                # 停手判据一条都没放松，只是搬进了内层：粘卡的每一笔照样 attempts += 1，
                # 所以 max_card_attempts 仍是硬上限，粘卡绕不过它。
                while True:
                    attempts += 1
                    card_last4 = str(num)[-4:]
                    last4 = card_last4

                    amount = recharge_cfg.pick_amount()
                    pay = adapter.top_up(session, wid, card, amount=amount,
                                         monitor=monitor_callback, should_stop=should_stop)
                    result = vars(pay)
                    # 记账、累计一律用**实扣金额**，不是请求金额。两者可能不同：opencode
                    # 首充的金额由站点定死（$20），我们传的随机额没有落点。适配器在
                    # PaymentResult.amount 里如实回报；它为 None 表示「没说」，才沿用请求额。
                    # 编排层刻意不去看 mode 自行推算——那是站点知识，不该焊进平台无关的骨架。
                    charged = result.get("amount")
                    if charged is None:
                        charged = amount
                        result["amount"] = charged
                    responses.append({"card_last4": card_last4, **result})

                    if pay.ok:
                        # 支付成功：标 paid + 记有效卡 + 账号状态 + 清失败计数 + 逐卡记账。
                        # 注意：paid 卡「不」永久消耗——paid 不在 NOT_SELECTABLE 内，后续仍可复选
                        # 复用；成功的卡也**不进冷却**，否则同一账号连充下去就无卡可用了。
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
                        if card_state_model:
                            # 「连续」失败才判废，成功一次就把之前攒的次数抹掉。
                            try:
                                card_state_model.reset_fail_streak(platform, num)
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
                        _log_card_attempt(card, True, "", result, charged)
                        paid_count += 1
                        session_topped += charged
                        last_paid4 = card_last4

                        # 余额上限：达到就换账号。两条判据**取或**，先判余额后判累计额：
                    #
                        #   balance_after >= cap   适配器报得出实时余额时的正解
                        #   session_topped >= cap  本次会话已投入的钱，兜底
                        #
                        # 两条都要，不能只留第一条。balance_after 是 Optional，infron 这类
                        # 读不到余额的平台永远是 None；更隐蔽的是「报得出但报得不对」——
                        # 只要有个适配器把 success 判成功却回了个陈旧或零的余额，第一条判据
                        # 就永远不成立，循环会一路刷到 max_attempts，单个账号能吃掉
                        # 8 × $100 = $800。加上第二条之后 balance_cap 才是**硬**上限，
                        # 不依赖任何适配器把余额读对。粘卡之后这条更要紧：同一张好卡不再受
                        # 「卡用完了」这个天然刹车约束，balance_cap 与 max_attempts 是仅有的两道。
                        #
                        # 余额低于上限但累计额超了，也该停：那说明账号在一边充一边烧
                        # credits，余额永远追不上上限，但我们的投入是实打实的。
                        #
                        # 判定结果先落在**本轮局部变量**上再赋给 stop_note：直接判
                        # `if stop_note` 的话，将来只要有人在循环里加一处不 break 的
                        # stop_note 赋值，之后每一笔成功都会被误判成「已达上限」。
                        cap = recharge_cfg.balance_cap
                        bal = result.get("balance_after")
                        cap_note = ''
                        if bal is not None and bal >= cap:
                            cap_note = f"余额 ${bal} 已达单账号上限 ${cap}"
                        elif session_topped >= cap:
                            cap_note = (f"本次已累计充值 ${session_topped:.0f}，"
                                        f"达单账号上限 ${cap}")
                        if cap_note:
                            stop_note = cap_note
                            if monitor_callback:
                                monitor_callback(session, f"{cap_note}，换下一个账号")
                            stop_all = True
                            break

                        # 粘卡续刷前重跑两道外层守卫。少了它们，一张一直过款的好卡会把
                        # max_card_attempts 和用户的停止请求一起架空——外层的检查这一轮
                        # 已经跑过了，而我们根本不打算回到外层。
                        #
                        # `max_attempts > 0` 一个都不能少：0 是「不限制」的哨兵值
                        # （opencode 当前就是 0），漏掉它 `1 >= 0` 恒真——第一笔一成功
                        # 就 break 换账号，每个账号只充一笔，balance_cap 形同虚设。
                        # 外层那道（见上面 max_attempts 定义处）一直是对的，唯独这道漏了，
                        # 于是全失败路径正常、一成功就停摆，账号停在首充的 $20。
                        if should_stop and should_stop():
                            raise InterruptedError("用户请求停止")
                        if max_attempts > 0 and attempts >= max_attempts:
                            errs.append(f"已达单次最多尝试 {max_attempts} 张卡上限，"
                                        f"停止以避免触发风控"
                                        f"（剩余 {len(cards) - idx - 1} 张未试）")
                            stop_note = errs[-1]
                            if monitor_callback:
                                monitor_callback(session, errs[-1])
                            stop_all = True
                            break

                        if monitor_callback:
                            monitor_callback(
                                session,
                                f"卡{card_last4} 充值 ${amount} 成功（本次第 {paid_count} 笔），"
                                f"这张卡能过款，继续用它充下一笔")
                        continue

                    outcome = result.get("outcome")
                    reason = f"卡{card_last4}: {outcome} - {result.get('err','')}"
                    errs.append(reason)

                    if outcome == "needs_captcha":
                        # hCaptcha 是账号/风控级拦截，换卡无用且会持续触发风控——立即停手。
                        # 不标卡无效、不写卡消耗日志，保留其余卡交人工过验证码后重试。
                        # 已成功的笔数照常算数，所以这里是 break 而不是 return——出口处
                        # 按 paid_count 决定返回 topup 还是 failed。
                        if monitor_callback:
                            monitor_callback(session, f"{reason}；需人工过 hCaptcha，停止换卡")
                        stop_note = "遇 hCaptcha 拦截，停止换卡"
                        stop_err = "hCaptcha 人机验证拦截，需人工完成后重试：" + reason
                        stop_all = True
                        break
                    elif outcome == "error":
                        # 页面/基础设施故障（未找到入口/选卡失败/填卡失败/点 Pay 失败），非卡问题：
                        # 不判无效、不冷却、不记账、不消耗——留着这张卡下次重试，避免因页面故障误烧卡。
                        if monitor_callback:
                            monitor_callback(session, f"{reason}；页面/基础设施异常，跳过不消耗此卡")
                    elif outcome == "unknown":
                        # 已点 Pay 提交，但超时内未确认到账，也无明确拒付/3DS/captcha 信号。不确定
                        # 是否卡的问题，保守处理：记一条失败日志留痕，但不改卡状态、不消耗、
                        # 也不计入连续失败次数，留待重试。
                        _log_card_attempt(card, False, reason, result, charged)
                    else:
                        # failed（明确拒付 / 3DS 交互挑战 / 3DS 认证失败）：
                        #   1. 无条件进冷却——同一张卡两次使用之间至少隔 fail_cooldown_hours，
                        #      免得在发卡行那里连着撞 velocity 风控；
                        #   2. 连续失败计数 +1，达到 max_fail_streak 才判废。
                        #
                        # 此前这里按「本平台是否成功过」分岔成「冷却 or 判废」。那个分岔已删除：
                        # 冷却对所有失败一视同仁，判废只看计数。好卡的豁免不靠这里查
                        # last_success_at，而是靠 mark_invalid_by_number 底层那道 valid_cards
                        # 守卫——它本就是所有「标无效」入口的最终收口，现在成了唯一收口。
                        streak = 0
                        if card_state_model:
                            try:
                                card_state_model.set_cooldown(
                                    platform, num, hours=recharge_cfg.fail_cooldown_hours,
                                    reason="充值失败，冷却")
                                streak = card_state_model.bump_fail_streak(platform, num)
                            except Exception:
                                streak = 0
                        if streak >= recharge_cfg.fail_threshold():
                            if card_pool_model:
                                try:
                                    card_pool_model.mark_invalid_by_number(platform, num)
                                except Exception:
                                    pass
                            if monitor_callback:
                                monitor_callback(
                                    session,
                                    f"卡{card_last4} 已连续失败 {streak} 次，判为无效")
                        elif monitor_callback and streak:
                            # 不报小时数：实际到期是 max(次日 00:00, now + N 小时)，
                            # 只说「冷却 12h」会与卡在列表页显示的到期时刻对不上。
                            monitor_callback(
                                session,
                                f"卡{card_last4} 连续失败 {streak}/{recharge_cfg.fail_threshold()} 次，"
                                f"当日不再使用，冷却至 "
                                f"{card_state_model.get_tds_until(platform, num) or '次日'}")
                        _log_card_attempt(card, False, reason, result, charged)

                    if monitor_callback:
                        monitor_callback(session, f"{reason}；尝试下一张卡（{idx+1}/{len(cards)}）")
                    # 这张卡不再过款（拒付 / 页面故障 / 未定案），粘不下去了 —— 退出内层，
                    # 由 finally 放开它，外层换下一张。
                    break
            finally:
                if payment_registry is not None:
                    payment_registry.release(num)

            # stop_all 由内层置位，必须等 finally 放开卡之后才生效：写在 try 里 break
            # 只会跳出内层的 while，外层照样接着换下一张卡。
            if stop_all:
                break

        # ── 单一出口 ──
        # 只要成功过一笔就算 topup，哪怕循环最后是被 hCaptcha 或试卡上限打断的。
        # _grab_apikey 放在这里而不是每笔成功后调：单账号连充 5 笔的话，放在循环里
        # 会白白多导航 4 次页面。
        if paid_count:
            # 收尾重读一次余额并落库，**以刷新后页面显示的数字为准**。
            #
            # 为什么不能沿用循环里那个 balance_after：它来自 detect_payment_result 的
            # _balance_grew()，那个函数在余额「第一次比原来大」的瞬间就返回并定案。
            # 那一刻页面上的数字未必是结算完的终值——2026-08-04 实测首充 opencode
            # 报 20.0，账号列表就一直显示 $20。R3 之后一个会话连充多笔，中间某笔的
            # 瞬时值被当成最终余额的概率更高。
            #
            # read_balance 内部会重新 session.get(billing 页) 再读，是一次干净的
            # 「刷新页面看余额」，不做任何加减推算——页面显示多少就是多少。
            # best-effort：读不到就保留循环里写的值，不清空。
            try:
                final_bal = adapter.read_balance(session, wid, monitor_callback)
                if final_bal is not None and platform_account_model:
                    platform_account_model.update_balance(platform, email, final_bal)
                    if monitor_callback:
                        monitor_callback(session, f"{email} 充值后余额 ${final_bal}")
            except Exception:
                pass
            _grab_apikey(session, wid)
            note = stop_note or "可选卡已试完"
            summary = f"本次成功充值 {paid_count} 笔、合计 ${session_topped:.0f}（{note}）"
            if monitor_callback:
                monitor_callback(session, summary)
            return (True, summary, responses, last_paid4 or last4, "topup")
        if stop_err:
            return (False, stop_err, responses, last4, "failed")
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
