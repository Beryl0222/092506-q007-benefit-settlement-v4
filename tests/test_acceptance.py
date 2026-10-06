"""固定数据验收测试。

覆盖验收场景：
1. 跨日补传与到达顺序无关；
2. 重复回调（request_id 幂等 + 无 request_id 的自然去重）；
3. 部分退款、退款晚于结账；
4. 活动版本切换；
5. 两家店争抢同一权益，争议冻结只影响涉事门店；
6. 撤销核销；
7. 崩溃恢复后余额与账单一致；对账、重算、人工解冻。
"""
import json
import os
import tempfile
import unittest

from benefit_settlement.api import handle
from benefit_settlement.domain import now_iso
from benefit_settlement.service import Service
from benefit_settlement.store import Store


def build_activity(service: Service) -> None:
    """两期活动：v1 自 2026-09-01 起补贴 80%，v2 自 2026-10-01 起补贴 60%。

    权益面值固定 1000 分：v1 结算 800 分，v2 结算 600 分。
    """
    service.publish_activity("ACT", 1, "COMM-1", 1000, "0.80",
                             "2026-09-01", request_id="pub-v1")
    service.publish_activity("ACT", 2, "COMM-1", 1000, "0.60",
                             "2026-10-01", request_id="pub-v2")


class 跨日补传测试(unittest.TestCase):
    def test_离线终端次日补传核销_按业务时间定生效日(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        # 核销发生在 09-20，终端离线，09-21 才补传
        r = s.redeem("E1", "SHOP-A", "red-offline-1",
                     "2026-09-20T18:30:00+00:00")
        self.assertFalse(r["duplicated"])
        self.assertEqual(r["settle_amount"], 800)
        [entry] = s.ledger(store_id="SHOP-A")
        self.assertEqual(entry["biz_date"], "2026-09-20")  # 归属业务日而非补传日
        self.assertEqual(entry["kind"], "settle")

    def test_到达顺序不同_最终账单一致(self):
        """同一组业务事实，在线/离线到达顺序不同，重放结果必须相同。"""
        def run(order: str) -> dict:
            s = Service(Store())
            build_activity(s)
            s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
            s.issue("E2", "ACT", "", "iss-2", "2026-09-21T10:00:00+00:00")
            redeem_a = ("E1", "SHOP-A", "r-a", "2026-09-20T12:00:00+00:00")
            redeem_b = ("E1", "SHOP-B", "r-b", "2026-09-20T11:00:00+00:00")  # B 更早
            if order == "online_first":
                s.redeem(*redeem_a)   # A 先到
                s.redeem(*redeem_b)   # B 补传后反超
            else:
                s.redeem(*redeem_b)
                s.redeem(*redeem_a)
            s.redeem("E2", "SHOP-B", "r-c", "2026-09-21T11:00:00+00:00")
            s.close_period("SHOP-A", "2026-09-30", "bill-a")
            s.close_period("SHOP-B", "2026-09-30", "bill-b")
            return {
                "E1": s.entitlement("E1"),
                "A": s.store_balance("SHOP-A"),
                "B": s.store_balance("SHOP-B"),
                "rejected": sorted(x["biz_no"] and x["reason"] for x in s.rejected()),
            }

        r1 = run("online_first")
        r2 = run("offline_first")
        self.assertEqual(r1["E1"]["winner_store"], "SHOP-B")
        # 胜者与金额不随到达顺序变化（内部落库序号允许不同）
        self.assertEqual((r1["E1"]["status"], r1["E1"]["winner_store"],
                          r1["E1"]["settle_amount"]),
                         (r2["E1"]["status"], r2["E1"]["winner_store"],
                          r2["E1"]["settle_amount"]))
        self.assertEqual(r1["A"]["active_amount"], r2["A"]["active_amount"])
        self.assertEqual(r1["B"]["active_amount"], r2["B"]["active_amount"])
        # E1 只在最早的 SHOP-B 生效一次；E2 也在 SHOP-B → B 合计 1600
        self.assertEqual(r1["B"]["active_amount"], 1600)
        self.assertEqual(r1["A"]["active_amount"], 0)


class 重复回调测试(unittest.TestCase):
    def test_相同request_id重放返回同一业务编号且只生效一次(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        first = s.redeem("E1", "SHOP-A", "cb-1", "2026-09-20T12:00:00+00:00")
        second = s.redeem("E1", "SHOP-A", "cb-1", "2026-09-20T12:00:00+00:00")
        third = s.redeem("E1", "SHOP-A", "cb-1", "2026-09-20T12:00:00+00:00")
        self.assertFalse(first["duplicated"])
        self.assertTrue(second["duplicated"])
        self.assertEqual(first["biz_no"], second["biz_no"])
        self.assertEqual(third["biz_no"], first["biz_no"])
        self.assertEqual(len(s.ledger(store_id="SHOP-A")), 1)

    def test_终端换request_id重发同一笔_自然指纹去重(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        first = s.redeem("E1", "SHOP-A", "term-1", "2026-09-20T12:00:00+00:00")
        # 终端重启后生成了新 request_id，但业务事实完全相同
        retry = s.redeem("E1", "SHOP-A", "term-1-retry",
                         "2026-09-20T12:00:00+00:00")
        self.assertTrue(retry["duplicated"])
        self.assertEqual(retry["dedup"], "natural_key")
        self.assertEqual(retry["biz_no"], first["biz_no"])
        self.assertEqual(len(s.ledger(store_id="SHOP-A")), 1)


class 退款测试(unittest.TestCase):
    def test_部分退款按比例冲减补贴(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        # 退面值的一半（500 分）→ 补贴冲减 400 分
        s.refund("E1", 500, "ref-1", "2026-09-22T09:00:00+00:00")
        ent = s.entitlement("E1")
        self.assertEqual(ent["status"], "redeemed")          # 部分退款仍可继续
        self.assertEqual(ent["refunded_face"], 500)
        self.assertEqual(ent["refunded_amount"], 400)
        self.assertEqual(s.store_balance("SHOP-A")["active_amount"], 800 - 400)

    def test_退款晚于结账_负向分录进入下一账单(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        bill1 = s.close_period("SHOP-A", "2026-09-30", "bill-1")
        self.assertEqual(bill1["amount"], 800)
        # 10-02 才发生退款（晚于 9 月结账）
        s.refund("E1", 500, "ref-late", "2026-10-02T09:00:00+00:00")
        bill2 = s.close_period("SHOP-A", "2026-10-03", "bill-2")
        self.assertEqual(bill2["amount"], -400)              # 下一账单抵扣
        # 已出账单不可变，门店累计净额 = 800 - 400
        balance = s.store_balance("SHOP-A")
        self.assertEqual(balance["settled_amount"], 400)
        self.assertEqual(balance["active_amount"], 400)
        self.assertTrue(s.reconcile()["ok"])

    def test_累计退款不得超过面值(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        s.refund("E1", 600, "ref-1", "2026-09-22T09:00:00+00:00")
        r = s.refund("E1", 500, "ref-2", "2026-09-23T09:00:00+00:00")
        self.assertTrue(r["warnings"])                       # 超额退款被拒绝
        self.assertEqual(s.entitlement("E1")["refunded_face"], 600)


class 活动版本切换测试(unittest.TestCase):
    def test_发行时刻决定版本_切换后新权益按新版本结算(self):
        s = Service(Store())
        build_activity(s)
        s.issue("OLD", "ACT", "", "iss-old", "2026-09-28T10:00:00+00:00")
        s.issue("NEW", "ACT", "", "iss-new", "2026-10-02T10:00:00+00:00")
        s.redeem("OLD", "SHOP-A", "r-old", "2026-09-28T12:00:00+00:00")
        s.redeem("NEW", "SHOP-A", "r-new", "2026-10-02T12:00:00+00:00")
        self.assertEqual(s.entitlement("OLD")["version"], 1)
        self.assertEqual(s.entitlement("NEW")["version"], 2)
        rows = s.ledger(store_id="SHOP-A")
        self.assertEqual({e["version"]: e["amount"] for e in rows}, {1: 800, 2: 600})

    def test_跨版本日的补传核销仍按权益发行时版本(self):
        s = Service(Store())
        build_activity(s)
        # 9 月发行，10 月才补传核销，补贴仍是 v1 的 800
        s.issue("E1", "ACT", "", "iss-1", "2026-09-30T20:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-10-01T08:00:00+00:00")
        self.assertEqual(s.entitlement("E1")["settle_amount"], 800)


class 争抢与冻结隔离测试(unittest.TestCase):
    def _scene(self) -> Service:
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.issue("E2", "ACT", "", "iss-2", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-a", "2026-09-20T12:00:00+00:00")
        s.redeem("E1", "SHOP-B", "r-b", "2026-09-20T11:00:00+00:00")  # B 更早
        s.redeem("E2", "SHOP-A", "r-c", "2026-09-20T13:00:00+00:00")  # A 正常业务
        return s

    def test_一笔权益只在最早门店生效_争议自动挂账(self):
        s = self._scene()
        self.assertEqual(s.entitlement("E1")["winner_store"], "SHOP-B")
        [dispute] = s.disputes("open")
        self.assertEqual(dispute["winner_store"], "SHOP-B")
        self.assertEqual(dispute["loser_store"], "SHOP-A")
        # 两家各自的 9 月账单：B 含 E1(800)，A 只含 E2(800)
        bill_a = s.close_period("SHOP-A", "2026-09-30", "bill-a")
        bill_b = s.close_period("SHOP-B", "2026-09-30", "bill-b")
        self.assertEqual(bill_a["amount"], 800)
        self.assertEqual(bill_b["amount"], 800)
        # 争议期间：B 账单中 E1 的 800 被冻结；A 的账单不受争议影响
        self.assertEqual(bill_b["frozen_amount"], 800)
        self.assertEqual(bill_a["frozen_amount"], 0)
        self.assertEqual(s.store_balance("SHOP-B")["available_amount"], 0)
        self.assertEqual(s.store_balance("SHOP-A")["available_amount"], 800)

    def test_人工解冻后金额可用(self):
        s = self._scene()
        s.close_period("SHOP-B", "2026-09-30", "bill-b")
        self.assertEqual(s.store_balance("SHOP-B")["available_amount"], 0)
        s.unfreeze("FRZ-AUTO-E1", "uf-1", "op-zhang",
                   occurred_at="2026-10-01T09:00:00+00:00",
                   entitlement_id="E1")
        # 解冻事件关闭争议 → 账单冻结额解除
        self.assertEqual(s.disputes("open"), [])
        self.assertEqual(s.store_balance("SHOP-B")["available_amount"], 800)
        self.assertTrue(s.reconcile()["ok"])

    def test_争议解决后再次争抢_争议重新打开并再次冻结(self):
        s = self._scene()
        s.close_period("SHOP-B", "2026-09-30", "bill-b")
        self.assertEqual(len(s.disputes("open")), 1)
        s.unfreeze("FRZ-AUTO-E1", "uf-1", "op",
                   occurred_at="2026-10-01T09:00:00+00:00", entitlement_id="E1")
        self.assertEqual(s.disputes("open"), [])
        # SHOP-C 拿出更早的核销凭证补传 → 新争议、新冻结
        s.redeem("E1", "SHOP-C", "r-d", "2026-09-20T10:30:00+00:00")
        [dispute] = s.disputes("open")
        self.assertEqual(dispute["winner_store"], "SHOP-C")
        self.assertEqual(dispute["loser_store"], "SHOP-B")
        # 新一轮冻结需要重新结账才能看到 held
        bill = s.close_period("SHOP-C", "2026-10-05", "bill-c")
        self.assertEqual(bill["frozen_amount"], 800)
        self.assertTrue(s.reconcile()["ok"])

    def test_门店日冻结_只冻指定门店指定日(self):
        s = self._scene()
        s.freeze("store_date", "门店巡检异常", "frz-1",
                 occurred_at="2026-10-01T09:00:00+00:00",
                 store_id="SHOP-A", biz_date="2026-09-20")
        bill_a = s.close_period("SHOP-A", "2026-09-30", "bill-a")
        bill_b = s.close_period("SHOP-B", "2026-09-30", "bill-b")
        self.assertEqual(bill_a["frozen_amount"], 800)
        self.assertEqual(bill_b["frozen_amount"], 800)   # B 仍因 E1 争议冻结
        # SHOP-A 在别的业务日新增核销不受影响
        s.issue("E3", "ACT", "", "iss-3", "2026-10-02T10:00:00+00:00")
        s.redeem("E3", "SHOP-A", "r-d", "2026-10-02T12:00:00+00:00")
        bill_a2 = s.close_period("SHOP-A", "2026-10-03", "bill-a2")
        self.assertEqual(bill_a2["amount"], 600)         # v2 版本
        self.assertEqual(bill_a2["frozen_amount"], 0)    # 10-02 不在冻结范围


class 撤销测试(unittest.TestCase):
    def test_撤销核销后权益恢复可核销_账单净额为零(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        red = s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        s.void_redeem("E1", "SHOP-A", red["biz_no"], "v-1",
                      "2026-09-20T13:00:00+00:00")
        self.assertEqual(s.entitlement("E1")["status"], "voided")
        # 撤销后可被（同一家或另一家）重新核销
        s.redeem("E1", "SHOP-B", "r-2", "2026-09-20T14:00:00+00:00")
        self.assertEqual(s.entitlement("E1")["winner_store"], "SHOP-B")
        # 撤给 A 的成对分录净额为 0
        a_rows = s.ledger(store_id="SHOP-A")
        self.assertEqual(sum(e["amount"] for e in a_rows), 0)
        self.assertTrue(all(e["status"] == "reversed" for e in a_rows))

    def test_撤销后才结账_不产生金额(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        red = s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        s.void_redeem("E1", "SHOP-A", red["biz_no"], "v-1",
                      "2026-09-20T13:00:00+00:00")
        bill = s.close_period("SHOP-A", "2026-09-30", "bill-empty")
        self.assertEqual(bill["amount"], 0)
        self.assertEqual(bill["entries"], [])

    def test_结账后撤销_原账单不变_下期抵扣(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        red = s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        bill1 = s.close_period("SHOP-A", "2026-09-30", "bill-1")
        self.assertEqual(bill1["amount"], 800)
        s.void_redeem("E1", "SHOP-A", red["biz_no"], "v-1",
                      "2026-10-02T10:00:00+00:00")
        bill2 = s.close_period("SHOP-A", "2026-10-03", "bill-2")
        self.assertEqual(bill2["amount"], -800)
        # 历史账单不可变，门店累计净额为 0
        self.assertEqual(s.bill(bill1["biz_no"])["amount"], 800)
        self.assertEqual(s.store_balance("SHOP-A")["settled_amount"], 0)


class 崩溃恢复测试(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.path)

    def test_事件已落库投影未更新_重启自动追平(self):
        s = Service(Store(self.path))
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        s.close()

        # 模拟崩溃：事件已追加但投影未重建（用独立连接直接落一条事件）
        import sqlite3
        raw = sqlite3.connect(self.path)
        raw.execute("BEGIN IMMEDIATE")
        seq = raw.execute("SELECT COALESCE(MAX(seq),0)+1 FROM event").fetchone()[0]
        raw.execute(
            """INSERT INTO event(seq, biz_no, request_id, type, entitlement_id,
                   activity_id, version, community_id, store_id, amount,
                   occurred_at, recorded_at, natural_key, payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (seq, f"RED-{seq:010d}", "r-crash", "redeemed", "E1", "ACT", 1,
             "COMM-1", "SHOP-B", 0, "2026-09-20T11:00:00+00:00", now_iso(),
             "crash-natural-key", "{}"),
        )
        raw.execute("COMMIT")
        raw.close()

        s2 = Service(Store(self.path))   # 启动即重放
        self.assertEqual(s2.entitlement("E1")["winner_store"], "SHOP-B")  # 更早者胜
        report = s2.reconcile()
        self.assertTrue(report["ok"], report["problems"])

    def test_重算接口修复被破坏的投影(self):
        s = Service(Store(self.path))
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        # 人为破坏投影
        s.store.connection.execute("DELETE FROM proj_ledger")
        s.store.connection.commit()
        self.assertFalse(s.reconcile()["ok"])
        result = s.recompute()
        self.assertTrue(result["ok"])
        self.assertEqual(result["replayed_events"], 4)
        self.assertTrue(s.reconcile()["ok"])
        self.assertEqual(s.store_balance("SHOP-A")["active_amount"], 800)


class 结账健壮性测试(unittest.TestCase):
    def test_结账混入非法分录_合法部分照常结账(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        bill = s.close_period("SHOP-A", "2026-09-30", "bill-1",
                              entry_ids=["1", "999"])
        self.assertTrue(bill.get("warnings"))
        self.assertEqual(bill["amount"], 800)
        self.assertEqual(bill["entries"], ["1"])

    def test_发行早于活动生效日被拒绝(self):
        s = Service(Store())
        s.publish_activity("ACT", 1, "COMM-1", 1000, "0.8",
                           "2026-09-01", request_id="p1")
        with self.assertRaises(ValueError):
            s.issue("E1", "ACT", "", "iss-early", "2026-08-30T10:00:00+00:00")

    def test_解冻不存在的冻结号_告警且不影响数据(self):
        s = Service(Store())
        build_activity(s)
        r = s.unfreeze("FRZ-NOPE", "uf-x", "op",
                       occurred_at="2026-10-01T09:00:00+00:00")
        self.assertTrue(r["warnings"])
        self.assertTrue(s.reconcile()["ok"])


class 非法命令测试(unittest.TestCase):
    def test_重复发行与重复活动版本被拒绝(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        again = s.issue("E1", "ACT", "", "iss-1-dup",
                        "2026-09-25T10:00:00+00:00")
        self.assertTrue(again["warnings"])
        # 原发行数据未被覆盖
        self.assertEqual(s.entitlement("E1")["status"], "issued")
        dup_act = s.publish_activity("ACT", 1, "COMM-1", 2000, "0.50",
                                     "2026-09-01", request_id="pub-dup",
                                     occurred_at="2026-09-25T00:00:00+00:00")
        self.assertTrue(dup_act["warnings"])
        s.issue("E2", "ACT", "", "iss-2", "2026-09-20T10:00:00+00:00")
        s.redeem("E2", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        # 新版本定义未生效，仍按原 80% 结算
        self.assertEqual(s.entitlement("E2")["settle_amount"], 800)

    def test_未核销不能退款_不能撤销别人的核销(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        r = s.refund("E1", 100, "ref-1", "2026-09-21T09:00:00+00:00")
        self.assertTrue(r["warnings"])
        s.redeem("E1", "SHOP-A", "r-1", "2026-09-20T12:00:00+00:00")
        bad = s.void_redeem("E1", "SHOP-B", "r-1", "v-1",
                            "2026-09-20T13:00:00+00:00")
        self.assertTrue(bad["warnings"])
        self.assertEqual(s.entitlement("E1")["winner_store"], "SHOP-A")


class 三家争抢测试(unittest.TestCase):
    def test_撤销胜者后争议在剩余两家间重建(self):
        s = Service(Store())
        build_activity(s)
        s.issue("E1", "ACT", "", "iss-1", "2026-09-20T10:00:00+00:00")
        # 时间：C(10:30) < B(11:00) < A(12:00)
        s.redeem("E1", "SHOP-A", "ra", "2026-09-20T12:00:00+00:00")
        s.redeem("E1", "SHOP-B", "rb", "2026-09-20T11:00:00+00:00")
        red_c = s.redeem("E1", "SHOP-C", "rc", "2026-09-20T10:30:00+00:00")
        self.assertEqual(s.entitlement("E1")["winner_store"], "SHOP-C")
        [d] = s.disputes("open")
        self.assertEqual(d["loser_store"], "SHOP-B")
        # C 撤销自己的核销 → B 补位，争议仍是 open（B vs A）
        s.void_redeem("E1", "SHOP-C", red_c["biz_no"], "vc",
                      "2026-09-20T14:00:00+00:00")
        self.assertEqual(s.entitlement("E1")["winner_store"], "SHOP-B")
        [d2] = s.disputes("open")
        self.assertEqual((d2["winner_store"], d2["loser_store"]),
                         ("SHOP-B", "SHOP-A"))
        # B 再撤销 → 只剩 A，争议消解
        s.void_redeem("E1", "SHOP-B", "rb", "vb",
                      "2026-09-20T15:00:00+00:00")
        self.assertEqual(s.disputes("open"), [])
        self.assertTrue(s.reconcile()["ok"])


class 并发争抢测试(unittest.TestCase):
    def test_两家店同时核销_数据库层保证只生效一次(self):
        import threading
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        try:
            bootstrap = Service(Store(path))
            build_activity(bootstrap)
            bootstrap.issue("E1", "ACT", "", "iss-1",
                            "2026-09-20T10:00:00+00:00")
            bootstrap.close()

            errors: list[Exception] = []

            def worker(store_id: str, request_id: str, when: str) -> None:
                try:
                    svc = Service(Store(path))
                    svc.redeem("E1", store_id, request_id, when)
                    svc.close()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=worker,
                                  args=("SHOP-A", "ra", "2026-09-20T12:00:00+00:00"))
            t2 = threading.Thread(target=worker,
                                  args=("SHOP-B", "rb", "2026-09-20T11:00:00+00:00"))
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(errors, [])

            svc = Service(Store(path))
            ent = svc.entitlement("E1")
            self.assertEqual(ent["winner_store"], "SHOP-B")   # 业务时间更早者胜
            self.assertEqual(ent["settle_amount"], 800)
            winners = [e for e in svc.ledger()
                       if e["kind"] == "settle" and e["status"] == "active"]
            self.assertEqual(len(winners), 1)                  # 只生效一次
            self.assertTrue(svc.reconcile()["ok"])
            svc.close()
        finally:
            os.unlink(path)

    def test_同一回调并发重放_只产生一条事件(self):
        import threading
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        try:
            bootstrap = Service(Store(path))
            build_activity(bootstrap)
            bootstrap.issue("E1", "ACT", "", "iss-1",
                            "2026-09-20T10:00:00+00:00")
            bootstrap.close()

            results: list[dict] = []
            lock = threading.Lock()
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    svc = Service(Store(path))
                    r = svc.redeem("E1", "SHOP-A", "same-cb",
                                   "2026-09-20T12:00:00+00:00")
                    with lock:
                        results.append(r)
                    svc.close()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(len({r["biz_no"] for r in results}), 1)
            svc = Service(Store(path))
            self.assertEqual(len(svc.ledger(store_id="SHOP-A")), 1)
            svc.close()
        finally:
            os.unlink(path)


class JSON接口测试(unittest.TestCase):
    def test_api_全链路与对账(self):
        svc = Service(Store())
        def call(body: dict):
            return json.loads(handle(json.dumps(body, ensure_ascii=False), svc))

        self.assertEqual(call({"action": "health"})["status"], "ok")
        call({"action": "publish_activity", "data": {
            "activity_id": "ACT", "version": 1, "community_id": "COMM-1",
            "face_value": 1000, "rate": "0.8", "effective_from": "2026-09-01",
            "request_id": "p1"}})
        r = call({"action": "issue", "data": {
            "entitlement_id": "E1", "activity_id": "ACT",
            "community_id": "", "request_id": "i1",
            "occurred_at": "2026-09-20T10:00:00+00:00"}})
        self.assertTrue(r["ok"])
        call({"action": "redeem", "data": {
            "entitlement_id": "E1", "store_id": "SHOP-A", "request_id": "x1",
            "occurred_at": "2026-09-20T12:00:00+00:00"}})
        dup = call({"action": "redeem", "data": {
            "entitlement_id": "E1", "store_id": "SHOP-A", "request_id": "x1",
            "occurred_at": "2026-09-20T12:00:00+00:00"}})
        self.assertTrue(dup["data"]["duplicated"])
        recon = call({"action": "reconcile"})
        self.assertTrue(recon["data"]["ok"])


if __name__ == "__main__":
    unittest.main()
