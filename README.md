# 线上线下消费权益发行与清分服务

面向「一刻钟生活圈」的权益运营：线下门店保留现场核销体验，线上渠道发放优惠权益。
服务解决三类典型难题：

- 核销终端离线后**跨日补传**，消息到达顺序不确定；
- 同一权益被两家门店**争抢核销**，回调可能重复投递；
- **退款晚于结算**、活动补贴费率在活动版本之间切换。

核心思路是**事件溯源 + 幂等收件箱 + 可重建投影**：发行、核销、撤销、退款都是
不可变事件，清分结果随时可以由事件流重放得到，崩溃恢复等价于「重放一遍」。

## 设计要点

| 问题 | 机制 |
| --- | --- |
| 重复回调 / 终端重试 | 每个写命令必须携带 `request_id`，收件箱保证同号重放返回同一业务编号与结果 |
| 离线补传换了 request_id | 事件携带「自然指纹」（类型+权益+门店+金额+业务时间+负载），同事实只入库一次 |
| 到达顺序不定 | 胜负只按业务发生时间 `occurred_at`（并列按落库序），与到达顺序无关 |
| 一笔权益只生效一次 | 重放投影只保留唯一胜者分录；败者记入 `proj_rejected` |
| 更早核销补传挤掉已结账胜者 | 原账单不可变，生成等额负向 `clawback` 分录进入门店下一账单抵扣；未结账则成对作废净额为 0 |
| 两店争抢 | 第二次核销在同事务内追加争议自动冻结（`FRZ-AUTO-*`），只冻结涉事权益的正向结算 |
| 退款晚于结算 | 负向退款分录按退款业务日自动进入下一账单，历史账单不修改 |
| 活动版本切换 | 权益发行时锁定活动版本与补贴费率；后续核销/退款始终按发行版本计算 |
| 崩溃恢复 | 事件追加与投影重建在一个 `BEGIN IMMEDIATE` 事务内原子提交；启动时发现事件比投影新会自动重放追平 |

- 金额一律为整数**分**，`Decimal` 四舍五入，无浮点误差；
- 业务编号格式：`ISS/RED/VOI/REF/SET/FRZ/UFR/ACT-<10位流水>`，随事件落库确定，可重放；
- 冻结范围支持 `entitlement`（单笔权益）与 `store_date`（某门店某业务日），互不株连。

## 目录

- `src/benefit_settlement/domain.py` — 事件模型、金额/时间工具
- `src/benefit_settlement/store.py` — SQLite：事件流、幂等收件箱、可重建投影表
- `src/benefit_settlement/projection.py` — 纯函数重放投影（争抢、冲销、退款、冻结规则）
- `src/benefit_settlement/service.py` — 应用服务：命令处理、自动冻结、对账、重算、人工解冻
- `src/benefit_settlement/api.py` — 进程内 JSON 适配层
- `tests/test_acceptance.py` — 固定数据验收测试（29 个用例）

## 接口

Python 直接调用：

```python
svc = Service(Store("settlement.db"))

svc.publish_activity("ACT", 1, "COMM-1", face_value=1000, rate="0.80",
                     effective_from="2026-09-01", request_id="pub-1")
svc.issue("E1", "ACT", "", request_id="iss-1",
          occurred_at="2026-09-20T10:00:00+00:00")          # 自动选生效版本
svc.redeem("E1", "SHOP-A", "red-1", "2026-09-20T12:00:00+00:00")
svc.void_redeem("E1", "SHOP-A", ref="RED-0000000005",
                request_id="void-1", occurred_at="2026-09-20T13:00:00+00:00")
svc.refund("E1", amount=500, request_id="ref-1",
           occurred_at="2026-10-02T09:00:00+00:00")        # 部分退款（面值的分）
svc.close_period("SHOP-A", "2026-09-30", "bill-1")         # 自动收齐待结账分录
svc.freeze("store_date", "巡检异常", "frz-1",
           store_id="SHOP-A", biz_date="2026-09-20")       # 手工冻结
svc.unfreeze("FRZ-AUTO-E1", "uf-1", operator="op-zhang",
             entitlement_id="E1")                          # 人工解冻并关闭争议

svc.store_balance("SHOP-A")     # 已结/冻结/可用/在途
svc.bill(biz_no)                # 账单及绑定分录
svc.disputes("open")            # 未决争议
svc.rejected()                  # 被拒核销与非法命令
svc.reconcile()                 # 对账：投影逐表比对 + 账单/冲销勾稽
svc.recompute()                 # 重算：清空投影，按事件流重建
```

JSON 入口（`api.handle`）动作名：`health / publish_activity / issue / redeem /
void / refund / close_period / freeze / unfreeze / entitlement / balance / bill /
bills / ledger / disputes / rejected / reconcile / recompute`。
写动作统一携带 `request_id` 与业务发生时间 `occurred_at`（离线补传时如实回填）。

## 清分规则

- 可结算金额 = 权益面值 × 发行版本补贴费率（如面值 1000 分 × 80% = 800 分）；
- 账单按**业务日**（`occurred_at` 的日期，UTC）归集，不是到达日；
- 门店账单金额 = 绑定的正向结算 − 退款/冲销负向分录，可为负（下期抵扣）；
- 账单一旦生成不可变；争议期间 `frozen_amount` 只统计命中的正向分录，
  `available = settled − frozen`，其他门店、其他业务日不受影响。

## 运行

```bash
# 测试（标准库，无需第三方依赖）
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 语法检查
python3 -m compileall src
```

验收用例覆盖：跨日补传归属业务日、两种到达顺序结果一致、request_id 与自然指纹双重
去重、部分退款、退款晚于结账的下期抵扣、活动版本切换、多店争抢与争议冻结隔离、
撤销（结账前/后、三家争抢占位）、多线程并发争抢与并发重放、崩溃后自动追平、
对账漂移检测与重算修复，以及重复发行/非法结账/超额退款等非法命令防护。
