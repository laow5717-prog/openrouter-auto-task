"""把注册失败的账号退回 `imported`，让下一轮重新注册 GitHub。

判定逻辑抽在这里，供**两个调用方共用**：
  - `src/api/routes.py` 的 POST /api/accounts/reset-imported（后台 UI）
  - `scripts/reset_failed_accounts_to_imported.py`（命令行）

抽出来的理由：下面那两条保护都是踩坑换来的领域知识，两边各写一份必然漂移，
而漂移的表现是「UI 上重置了一批账号，跑起来发现全领不走」——不报错，只是白跑。
"""

import os

# 视为「注册没成、值得重来一次」的身份状态。
#
# failed    注册流程未走完
# pending   碰上 Arkose 人机验证，主动跳过
#
# **suspended 刻意不在其中。** 那是「注册出来就被 GitHub 挂起」，同一个邮箱重注册
# 大概率还是同样下场，退回 imported 只会让它每轮白跑一次。命令行脚本用
# --include-suspended 显式覆盖；UI 不提供这个口子——它是需要人想清楚才该做的事。
RESETTABLE = ('failed', 'pending')

# 重置为 imported 后，账号要能被 app.run_daily_pipeline._registerable_imported() 领走。
# 那个判据要求两件事：identity_status == 'imported'，且 _hotmail_for_account 取得到
# 收码数据。第二条正是下面 _has_mailbox 要复刻的。


def load_hotmail_emails(base_dir=None):
    """hotmail.xlsx 里的邮箱集合。

    **文件不存在时返回空集，不是错误。** 多数账号的收码链接随邮箱一起入库在
    accounts.email_verify_link，xlsx 只是第二来源（订阅任务那批只在 xlsx 的账号）。
    本机实测 hotmail.xlsx 根本不存在而 38 个 failed 账号全都有 email_verify_link——
    若把缺失当错误或当作「无收码数据」，这个功能会对当前数据 100% 失效。
    """
    try:
        from src.services.hotmail_inbox import read_hotmail_accounts
        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
        return {a.email for a in read_hotmail_accounts(
            os.path.join(base_dir, 'hotmail.xlsx'))}
    except Exception:
        return set()


def _has_mailbox(acct, hotmail_emails):
    """该账号能不能拿到注册收码数据。与 app._hotmail_for_account 同源判据。"""
    if (acct.get('email_verify_link') or '').strip():
        return True
    return acct.get('email') in hotmail_emails


def classify_for_reset(accounts, hotmail_emails, statuses=RESETTABLE):
    """把账号分三组：(可重置, 状态不符, 无收码数据)。

    纯函数——不碰 DB、不碰 Flask、不读文件，喂 dict 列表即可测试。

    accounts:       [{email, identity_status, email_verify_link}, ...]
    hotmail_emails: load_hotmail_emails() 的结果
    statuses:       视为可重置的状态，默认 RESETTABLE（脚本可传入含 suspended 的版本）

    「无收码数据」单独成一组而不是并进「状态不符」：两者的处理方式完全不同——
    状态不符是「你选错账号了」，无收码数据是「这账号得先补收信链接」。合并上报会让
    用户以为是同一个问题。
    """
    ready, bad_status, no_mailbox = [], [], []
    for acct in accounts:
        if (acct.get('identity_status') or '') not in statuses:
            bad_status.append(acct)
        elif not _has_mailbox(acct, hotmail_emails):
            no_mailbox.append(acct)
        else:
            ready.append(acct)
    return ready, bad_status, no_mailbox
