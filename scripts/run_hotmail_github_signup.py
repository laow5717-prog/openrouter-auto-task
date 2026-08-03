"""用 hotmail.xlsx 的真实邮箱跑 GitHub 半自动注册，并把结果落到 accounts 表。

注册需人工过 Arkose 验证码，故为**有头半自动**，逐个账号串行处理（--all 亦然）。

用法:
    # 只把 xlsx 数据导入 accounts 表（status='imported'），不起浏览器
    python3 scripts/run_hotmail_github_signup.py --import

    # 对第 N 个账号跑半自动注册（有头，你在场手动过码）
    python3 scripts/run_hotmail_github_signup.py --index 1

    # 对指定邮箱跑
    python3 scripts/run_hotmail_github_signup.py --email carold030@hotmail.com

    # 遍历全部（每个都要你在场过码）
    python3 scripts/run_hotmail_github_signup.py --all
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.hotmail_inbox import read_hotmail_accounts
from src.services.github_signup_service import signup_one
from src.models.database import Database
from src.models.account import AccountModel

_DEFAULT_XLSX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hotmail.xlsx")

# outcome → accounts.identity_status（全是 GitHub 侧结果，与平台无关）。
# signup_complete/account_suspended 时账号已建，补写 github 密码。
_OUTCOME_STATUS = {
    "signup_complete": "registered",
    "account_suspended": "suspended",
    "rejected_by_github": "rejected",
    "captcha_timeout": "failed",
    "no_verification_email": "failed",
    "verification_failed": "failed",
    "reached_captcha": "pending",
    "reached_verify_email": "pending",
    "error": "failed",
}
# 账号已在 GitHub 侧建成（凭据可用）的 outcome——落库时补写 login_password。
_ACCOUNT_CREATED = {"signup_complete", "account_suspended"}


def _mask(pw):
    if not pw:
        return ""
    return pw[:2] + "*" * max(0, len(pw) - 4) + pw[-2:] if len(pw) > 4 else "***"


def _import_all(account_model, accounts):
    print(f"导入 {len(accounts)} 个 hotmail 账号到 accounts 表（identity_status='imported'）...")
    for acc in accounts:
        # login_password 此时未知（GitHub 注册才生成）；email_password 存 hotmail 密码；
        # email_verify_link 存 ruoanzhu 收信链接，导入时一并落库。
        account_model.upsert(acc.email, login_password=None,
                             email_password=acc.password, identity_status="imported",
                             email_verify_link=acc.link)
        print(f"  ✓ {acc.email}  pw={_mask(acc.password)}")
    print("导入完成。")


def _persist_result(account_model, acc, result):
    """把一次注册结果落库。成功/挂起补写 GitHub 密码，其余仅更新状态。"""
    outcome = result.get("outcome", "error")
    status = _OUTCOME_STATUS.get(outcome, "failed")
    if outcome in _ACCOUNT_CREATED:
        account_model.upsert(acc.email,
                             login_password=result.get("github_password"),
                             email_password=acc.password, identity_status=status)
    else:
        # 确保行存在（可能跳过了 --import 直接注册），再更新状态。
        account_model.upsert(acc.email, login_password=None,
                             email_password=acc.password, identity_status="imported")
        account_model.update_identity_status(acc.email, status)
    print(f"  💾 已落库：{acc.email} → identity_status='{status}'"
          + ("（含 GitHub 凭据）" if outcome in _ACCOUNT_CREATED else ""))


def _run_one(account_model, acc):
    print(f"\n{'#'*60}\n# 注册 {acc.email}\n{'#'*60}")
    result = signup_one(headless=False, semi_auto=True, account=acc, then_opencode=True)
    print("\n结果:")
    print(json.dumps({k: result.get(k) for k in
                      ("ok", "email", "outcome", "reason", "final_url")},
                     ensure_ascii=False, indent=2))
    oc = result.get("opencode")
    if oc:
        print(f"opencode: ok={oc.get('ok')} wid={oc.get('wid')} go={oc.get('go_url')}")
    _persist_result(account_model, acc, result)
    return result


def main():
    parser = argparse.ArgumentParser(description="hotmail GitHub 半自动注册 + 落库")
    parser.add_argument("--xlsx", default=_DEFAULT_XLSX)
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="只导入 xlsx 到 accounts 表，不注册")
    parser.add_argument("--index", type=int, help="注册第几个账号（1 起）")
    parser.add_argument("--email", help="注册指定邮箱")
    parser.add_argument("--all", action="store_true", help="遍历全部账号逐个注册（需人在场过码）")
    args = parser.parse_args()

    accounts = read_hotmail_accounts(args.xlsx)
    if not accounts:
        print("❌ 未从 xlsx 解析出任何账号")
        sys.exit(1)
    print(f"从 {args.xlsx} 解析到 {len(accounts)} 个账号")

    db = Database()
    account_model = AccountModel(db)

    if args.do_import:
        _import_all(account_model, accounts)
        return

    # 选定要注册的账号集合
    if args.email:
        targets = [a for a in accounts if a.email == args.email]
        if not targets:
            print(f"❌ xlsx 中未找到邮箱 {args.email}")
            sys.exit(1)
    elif args.all:
        targets = accounts
    elif args.index:
        idx = args.index
        if idx < 1 or idx > len(accounts):
            print(f"❌ --index 越界（1..{len(accounts)}）")
            sys.exit(1)
        targets = [accounts[idx - 1]]
    else:
        print("请指定 --import / --index N / --email X / --all 之一")
        parser.print_help()
        sys.exit(2)

    print(f"将注册 {len(targets)} 个账号（半自动，需你在场手动过 Arkose 验证码）")
    results = [_run_one(account_model, acc) for acc in targets]

    ok = sum(1 for r in results if r.get("outcome") == "signup_complete")
    print(f"\n{'='*60}\n汇总：共 {len(results)} 个，成功 {ok} 个。")


if __name__ == "__main__":
    main()
