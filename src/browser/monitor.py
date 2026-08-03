"""进度回调工具。

平台适配器与支付供应商层都要在关键步骤上刷新 UI 截图并记一行进度，两边都依赖它，
所以放在基础设施层——否则 payments 得反过来 import platforms，层次就倒了。
"""


def step(monitor, session, msg):
    """刷新 UI 截图 + 记录步骤；monitor 在停止请求时会抛 InterruptedError。"""
    if monitor:
        monitor(session, msg)
    else:
        session.capture_frame()
