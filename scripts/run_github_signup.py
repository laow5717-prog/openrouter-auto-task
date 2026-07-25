"""GitHub 自动注册运行入口。

用法:
    python3 scripts/run_github_signup.py             # 有头，跑到 Arkose 验证码为止（默认）
    python3 scripts/run_github_signup.py --headless  # 伪无头
    python3 scripts/run_github_signup.py --semi-auto # 半自动：跑到验证码后暂停等人工手动过码，
                                                     #   过码后自动收 mail.tm 验证邮件、回填 launch
                                                     #   code、判定注册完成。需有头 + 人在场过码。
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.github_signup_service import signup_one


def main():
    parser = argparse.ArgumentParser(description="GitHub 自动注册（mail.tm 临时邮箱）")
    parser.add_argument("--headless", action="store_true", help="使用伪无头模式")
    parser.add_argument("--semi-auto", action="store_true",
                        help="半自动：跑到验证码后暂停等人工手动过码，过码后自动收邮件回填验证码（需有头 + 人在场）")
    parser.add_argument("--keep-open", action="store_true",
                        help="流程跑完后不关浏览器，进程挂住供你查看最终页面（Ctrl-C 结束）")
    args = parser.parse_args()

    if args.semi_auto and args.headless:
        print("⚠️  --semi-auto 需要有头模式手动过码，已忽略 --headless")
        args.headless = False

    result = signup_one(headless=args.headless, semi_auto=args.semi_auto,
                        keep_open=args.keep_open)

    print("\n" + "=" * 50)
    print("结果:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 50)

    # outcome=reached_captcha 视为流程跑通（退出码 0）；rejected/error 退出码 1
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
