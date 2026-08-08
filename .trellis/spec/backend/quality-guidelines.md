# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

(To be filled by the team)

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

(To be filled by the team)

---

## Required Patterns

<!-- Patterns that must always be used -->

(To be filled by the team)

---

## Testing Requirements

### 排序/优先级类的测试，必须先证明它会红

改了排序键、优先级、档位这类逻辑之后，**把改动反向变异一次，确认测试真的会失败**。
不这么做的话，「34 项全绿」什么都不能证明。

2026-08-08 的现场：给环境回收加了「recharged 排在真终态之后」这一档，配了 8 项行为测试，
全绿。把档位常量改回合并状态（模拟原 bug），**仍然 34 项全绿**——因为：

- `ORDER BY` 是多层的。第一层（档位）失效时，后面的层（余额降序、`last_used_at` LRU）
  会接管，而测试构造的数据恰好让后面的层给出同样的结果。
- 行为层断言（「谁先被删」）测的是整条排序链的合成结果，任何一层都能让它变绿。

两条对策：

1. **直接断言那个判据本身**。上例里加一条 `assert ranks[done] < ranks[recharged]`——
   不依赖余额、不依赖时间戳，是「两档没合并」的唯一无歧义证据。
2. **行为测试要构造成「其它层会给出相反结果」**。上例中 `recharged` 必须先创建，
   让它的 `last_used_at` 更早；这样档位合并时 LRU 会先删它（红），只有分档才会先删
   真终态（绿）。反过来构造则恒绿。

变异要选对：改常量的**值**（让 A 也落进 B 档）往往只是让行为更保守，模拟不出原 bug；
要改的是**分类逻辑本身**（让 B 落回 A 档）。选错变异会得到「测试抓不住」的假结论。

### 端点测试必须掐断外部服务

开发机上 AdsPower 客户端是常驻的。用 Flask `test_client` 打一个会碰
`_release_adspower_for` 的端点时，**它会真的连上本机 AdsPower 并调删除接口**——测试
不报错、看起来是绿的，但已经在动真实环境。

`tests/test_account_delete_adspower.py` 有现成的隔离基建，新测试直接复用，别重建：

```python
def _wire(monkeypatch, app, enabled=True, pool=None):
    state = app.config['APP_STATE']
    monkeypatch.setattr(type(state), 'adspower_enabled',
                        property(lambda self: enabled), raising=False)
    monkeypatch.setattr(type(state), '_ensure_adspower',
                        lambda self: (None, pool), raising=False)
```

只验状态流转、不关心环境的测试，把 `adspower_enabled` 直接设成 `False` 即可。

判断方法：端点摸到了 `AppState._ensure_adspower` / `_release_adspower_for` /
任何 `requests` 调用，就必须 monkeypatch。**测试通过不等于测试干净。**

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)
