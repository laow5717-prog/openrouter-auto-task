"""
OpenRouter 注册 & 绑卡 / 充值核心业务逻辑 —— 站点流程存根（占位）。

本模块原为 Cloudflare 站点的注册/绑卡/充值编排。项目改造为 OpenRouter 后，
Cloudflare 专属的页面流程（注册页、Turnstile、Stripe 绑卡、AI Gateway 充值等）
已从编排层剥离。以下 4 个公共函数保留原有签名与返回契约说明，供 app.py / routes.py
的上层编排与并发调度继续 import 与调用；函数体统一抛 NotImplementedError，等待
按 OpenRouter 站点实际流程逐个接入。

接入时对照 design.md / prd.md 的站点耦合面，把 driver.py 中标记为
`LEGACY Cloudflare-specific` 的浏览器方法替换为 OpenRouter 版实现，再在此填充编排。
原 Cloudflare 实现保留在本文件的 git 历史中，可作为接入参考。
"""

_NOT_IMPLEMENTED = (
    "OpenRouter 站点流程待接入：{name}。当前为框架存根，尚未实现 OpenRouter 的"
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
                     invoice_daily_cap=None, invoice_state_model=None,
                     payment_registry=None):
    """登录已有账号并充值。

    原返回契约:
        全量模式 (invoice_daily_cap is None): (bool, str, list, str, str)
        单步模式 (invoice_daily_cap 为整数):   (bool, str, list, str, str, dict)
    """
    raise NotImplementedError(_NOT_IMPLEMENTED.format(name="recharge_account"))
