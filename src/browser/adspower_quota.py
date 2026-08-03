"""AdsPower 环境配额的按平台仲裁。

## 为什么需要它

在此之前，代码里**根本没有配额上限的概念**——12 这个数字只存在于注释和文档里，
真正的配额是「被动发现」的：调 create_profile 撞了才抛 AdsPowerQuotaExceeded。
单平台跑的时候这样也能用，撞墙就整批收敛，反正下一个账号必然撞同一堵墙。

多平台并发之后这套不成立了。配额是**两个平台共用的物理资源**，而运行状态已经
按平台拆开，于是「撞墙就停自己」会退化成：A 平台饿死在等待，B 平台反复抛错自杀。
必须有一个共同的仲裁者。

## 硬上限为什么是 11 而不是 12

AdsPower 的配额是 12，但它自带一个 "Default Profile" 也占一个名额。实测配额会
卡在 11/12——第 12 个建不出来。所以可用的是 11。

## 借用与归还

平台各有 RESERVED 额度（opencode 7 / infron 4），加起来正好等于硬上限。
对方空闲时可以借，但总数永远受 TOTAL 约束。

归还是**协作式**的，不是抢占式：原主需要额度时，向借用方发一个归还请求；
借用方在**下一个账号边界**（当前账号跑完、释放环境时）不再申请新额度，直到还清。
原主在此期间等待。

为什么不做强制抢占：强行回收意味着中断一个正在跑的浏览器会话，而那可能是一笔
已提交、待授权的支付——中断会留下「钱可能扣了但状态不明」的不确定状态，这正是
PaymentResult 里 unknown 这个 outcome 存在的原因。省下的几十秒不值得换这个风险。
"""

import threading
import time


class AdsPowerQuota:
    """按平台的软配额：可借用、协作式归还、总数硬上限。

    线程安全。所有状态都在一把锁下，用 Condition 让等待方在额度释放时被唤醒，
    而不是轮询。

    ⚠️ 与 AdsPowerProfilePool._lock 的**顺序**：本仲裁器的锁必须在**外层**。
    池锁串行化「挑代理→建环境→撞配额→回收→重试」整条链，如果持着池锁再来等配额，
    释放方永远拿不到池锁去删环境 —— 直接死锁。调用方必须先 acquire 再进池。
    """

    TOTAL = 11
    DEFAULT_RESERVED = {'opencode': 7, 'infron': 4}

    def __init__(self, total=None, reserved=None):
        self.total = int(total) if total else self.TOTAL
        self.reserved = dict(reserved if reserved is not None else self.DEFAULT_RESERVED)
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._held = {}          # platform -> 当前占用数
        self._recall = {}        # platform -> 被要求归还的份数

    # ---------- 只读视图 ----------

    def held(self, platform=None):
        with self._lock:
            if platform is None:
                return dict(self._held)
            return self._held.get(platform, 0)

    def total_held(self):
        with self._lock:
            return sum(self._held.values())

    def reserved_for(self, platform):
        """该平台的自有额度。未在 reserved 里声明的平台按「均分剩余」处理——
        新接平台时忘了配也不会让它拿不到任何额度。"""
        if platform in self.reserved:
            return self.reserved[platform]
        rest = max(0, self.total - sum(self.reserved.values()))
        return rest

    def recall_pending(self, platform):
        """本平台被要求归还的份数。借用方在账号边界读它，决定要不要停下来还。"""
        with self._lock:
            return self._recall.get(platform, 0)

    # ---------- 核心 ----------

    def _can_take_locked(self, platform):
        """在锁内判断此刻能否再拿一个。返回 (能否, 原因)。"""
        held = self._held.get(platform, 0)
        total = sum(self._held.values())

        if total >= self.total:
            return False, f'总占用已达上限 {self.total}'

        # 自有额度内：随时可取，不受归还请求影响（本来就是自己的）
        if held < self.reserved_for(platform):
            return True, ''

        # 已超出自有额度 = 在借用。被要求归还时不能再借。
        if self._recall.get(platform, 0) > 0:
            return False, '正在归还借用的额度，暂不再借'

        return True, ''

    def acquire(self, platform, timeout=300, should_stop=None):
        """占一个额度。拿到返回 True。

        拿不到时**等待**而不是报错——配额是共用资源，对方跑完就会释放，
        直接判失败会让账号白白进失败集合。超时或被叫停才返回 False。

        should_stop: 可选的中断回调，用户点停止时能立刻退出，不必等满 timeout。
        """
        deadline = time.time() + max(0, timeout)
        with self._cv:
            while True:
                ok, _why = self._can_take_locked(platform)
                if ok:
                    self._held[platform] = self._held.get(platform, 0) + 1
                    return True
                if should_stop is not None and should_stop():
                    return False
                remain = deadline - time.time()
                if remain <= 0:
                    return False
                # 带超时地等：既能被 release 唤醒，也能周期性回来查 should_stop
                self._cv.wait(min(remain, 1.0))

    def release(self, platform):
        """还一个额度。归还请求会随之递减。"""
        with self._cv:
            cur = self._held.get(platform, 0)
            if cur <= 0:
                return
            self._held[platform] = cur - 1
            if self._recall.get(platform, 0) > 0:
                self._recall[platform] -= 1
                if self._recall[platform] <= 0:
                    self._recall.pop(platform, None)
            self._cv.notify_all()

    def request_recall(self, requester):
        """requester 想用自己的额度，但被别人借走了 —— 向借用方发归还请求。

        返回被要求归还的总份数（0 表示没人欠它）。

        只发请求、不强制回收：借用方会在下一个账号边界停下来还，
        中断正在进行的付款是不可接受的（见模块 docstring）。
        """
        with self._cv:
            need = self.reserved_for(requester) - self._held.get(requester, 0)
            if need <= 0:
                return 0
            # 只找**确实超出了自己额度**的平台要
            asked = 0
            for plat, held in self._held.items():
                if plat == requester:
                    continue
                over = held - self.reserved_for(plat)
                if over <= 0:
                    continue
                want = min(over, need - asked)
                if want <= 0:
                    break
                self._recall[plat] = max(self._recall.get(plat, 0), want)
                asked += want
                if asked >= need:
                    break
            return asked

    def snapshot(self):
        """给日志/接口看的完整状态。"""
        with self._lock:
            return {
                'total': self.total,
                'total_held': sum(self._held.values()),
                'reserved': dict(self.reserved),
                'held': dict(self._held),
                'recall': dict(self._recall),
            }
