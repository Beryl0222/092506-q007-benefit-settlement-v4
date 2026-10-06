"""事件流重放投影（纯函数）。

清分的所有结果都由 :class:`Projection` 对不可变事件流按 ``seq`` 重放得到，
与消息是在线实时到达还是离线补传无关：

- 同一权益多家门店核销 → 按业务发生时间 ``occurred_at`` 定胜负（并列时按落库序），
  败者分录记入 rejected，胜败双方记入争议表，争议未决期间胜者金额 held；
- 撤销/迟到的更早核销会触发「卫冕者更替」：旧分录冲销，已结账的生成待抵扣分录，
  未结账的成对作废，净额恒为零；
- 部分退款按面值比例生成负向分录，晚于结账的退款自动落到下一账单；
- 冻结只影响命中范围（某权益 或 某门店某日）的分录，不波及其他门店。
"""
from __future__ import annotations

import json

from .domain import (
    ACTIVITY_PUBLISHED, FROZEN, ISSUED, PERIOD_SETTLED, REDEEMED,
    REFUNDED, UNFROZEN, VOIDED, business_date, rate_mul,
)


class Projection:
    def __init__(self) -> None:
        self.seq = 0
        self.activities: dict[tuple[str, int], dict] = {}
        self.entitlements: dict[str, dict] = {}
        self.ledger: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.freezes: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {}
        self.rejected: list[dict] = []
        self.invalid: list[dict] = []
        # 运行期索引
        self._redeems: dict[str, list[dict]] = {}
        self._refund_face: dict[str, int] = {}
        self._bound: dict[str, str] = {}          # ledger key -> settlement biz_no
        self._next_id = 0

    def _alloc_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- 入口 ---------------------------------------------------------------

    def apply_all(self, events) -> "Projection":
        for event in events:
            self.apply(event)
        return self

    def apply(self, event) -> None:
        self.seq = event.seq
        t = event.type
        if t == ACTIVITY_PUBLISHED:
            key = (event.activity_id, event.version)
            if key in self.activities:
                self._invalid(event, "活动版本已存在，不可覆盖")
            else:
                self.activities[key] = {
                    "activity_id": event.activity_id,
                    "version": event.version,
                    "community_id": event.community_id,
                    "face_value": event.amount,
                    "rate": event.payload.get("rate", "1"),
                    "effective_from": event.payload.get("effective_from", ""),
                }
        elif t == ISSUED:
            if event.entitlement_id in self.entitlements:
                self._invalid(event, "权益已发行，不可重复发行")
            else:
                self.entitlements[event.entitlement_id] = {
                    "entitlement_id": event.entitlement_id,
                    "activity_id": event.activity_id,
                    "version": event.version,
                    "face_value": event.amount,
                    "status": "issued",
                    "winner_store": "",
                    "redeem_seq": 0,
                    "settle_amount": 0,
                    "refunded_face": 0,
                    "refunded_amount": 0,
                }
                self._redeems.setdefault(event.entitlement_id, [])
        elif t == REDEEMED:
            self._on_redeem(event)
        elif t == VOIDED:
            self._on_void(event)
        elif t == REFUNDED:
            self._on_refund(event)
        elif t == PERIOD_SETTLED:
            self._on_settle(event)
        elif t == FROZEN:
            self.freezes[event.biz_no] = {
                "biz_no": event.biz_no,
                "event_seq": event.seq,
                "scope": event.payload.get("scope", ""),
                "entitlement_id": event.entitlement_id,
                "store_id": event.store_id,
                "biz_date": event.payload.get("biz_date", ""),
                "reason": event.payload.get("reason", ""),
                "active": 1,
                "unfreeze_biz_no": "",
            }
        elif t == UNFROZEN:
            self._on_unfreeze(event)

    # -- 核销与争抢 ----------------------------------------------------------

    def _on_redeem(self, event) -> None:
        ent = self.entitlements.get(event.entitlement_id)
        if ent is None:
            self._invalid(event, "核销了不存在的权益")
            return
        if ent["status"] == "refunded":
            self._invalid(event, "权益已全额退款，不能再核销")
            return
        lst = self._redeems.setdefault(event.entitlement_id, [])
        lst.append({
            "seq": event.seq, "biz_no": event.biz_no,
            "request_id": event.request_id, "store_id": event.store_id,
            "occurred_at": event.occurred_at, "voided": False, "void_seq": 0,
            "entry_id": 0,
        })
        self._recompute_winner(ent, trigger_seq=event.seq)
        if len({r["store_id"] for r in lst if not r["voided"]}) >= 2:
            self._note_dispute(ent)

    def _on_void(self, event) -> None:
        ent = self.entitlements.get(event.entitlement_id)
        if ent is None:
            self._invalid(event, "撤销了不存在的权益")
            return
        ref = event.payload.get("ref", "")
        lst = self._redeems.get(event.entitlement_id, [])
        target = None
        for r in lst:
            if r["biz_no"] == ref or r["request_id"] == ref:
                target = r
                break
        if target is None or target["voided"]:
            self._invalid(event, "撤销无对应核销")
            return
        if target["store_id"] != event.store_id:
            self._invalid(event, "只能撤销本店核销")
            return
        target["voided"] = True
        target["void_seq"] = event.seq
        # 若撤销的是当前胜者，冲销其分录
        if ent["winner_store"] == target["store_id"] and ent["redeem_seq"] == target["seq"]:
            self._reverse_entry(
                ent, key=str(target["entry_id"]), trigger=event, reason="核销撤销",
            )
            target["entry_id"] = 0
            ent["winner_store"] = ""
            ent["redeem_seq"] = 0
            ent["settle_amount"] = 0
        self._recompute_winner(ent, trigger_seq=event.seq)
        if ent["status"] in ("redeemed", "refunded") and not ent["winner_store"]:
            ent["status"] = "voided"
        # 重新评估争议：仍有两家在争 → 刷新争议（胜者可能已补位）；
        # 只剩一家 → 争议消解并释放系统自动冻结（手工冻结保留）
        alive_stores = {r["store_id"] for r in lst if not r["voided"]}
        if len(alive_stores) >= 2:
            self._note_dispute(ent)
        else:
            disp = self.disputes.get(event.entitlement_id)
            if disp and disp["status"] == "open":
                disp["status"] = "resolved"
                self._release_auto_freeze(event.entitlement_id)

    def _recompute_winner(self, ent, trigger_seq: int) -> None:
        """在全部未撤销核销中，按 (业务时间, 落库序) 选唯一胜者，处理更替/补位。"""
        lst = self._redeems.get(ent["entitlement_id"], [])
        alive = [r for r in lst if not r["voided"]]
        if not alive:
            return
        winner = min(alive, key=lambda r: (r["occurred_at"], r["seq"]))
        if ent["redeem_seq"] == winner["seq"]:
            return
        # 存在旧胜者 → 更替（被更早上报时间的核销挤掉）
        if ent["redeem_seq"]:
            old = next(r for r in lst if r["seq"] == ent["redeem_seq"])
            self._reverse_entry(
                ent, key=str(old["entry_id"]),
                trigger_event_seq=trigger_seq, loser_store=old["store_id"],
                reason="争抢失败：更早核销生效",
            )
            old["entry_id"] = 0
        # 新胜者此前没有结算分录（被拒过 / 首次当选）→ 建分录
        if not winner["entry_id"]:
            amount = rate_mul(ent["face_value"],
                              self.activities[(ent["activity_id"], ent["version"])]["rate"])
            entry = self._entry(
                event_seq=winner["seq"], biz_no=winner["biz_no"], ent=ent,
                store_id=winner["store_id"],
                biz_date=business_date(winner["occurred_at"]),
                kind="settle", amount=amount,
            )
            winner["entry_id"] = entry["entry_id"]
        ent["winner_store"] = winner["store_id"]
        ent["redeem_seq"] = winner["seq"]
        ent["settle_amount"] = rate_mul(
            ent["face_value"],
            self.activities[(ent["activity_id"], ent["version"])]["rate"],
        )
        if ent["status"] == "issued" or ent["status"] == "":
            ent["status"] = "redeemed"

    def _reverse_entry(self, ent, key: str, reason: str,
                       trigger=None, trigger_event_seq: int = 0,
                       loser_store: str = "") -> None:
        """冲销胜者分录：已结账的留下待抵扣负向分录，未结账的成对作废。"""
        original = self.ledger.get(key)
        if original is None:
            return
        seq = trigger.seq if trigger is not None else trigger_event_seq
        trigger_date = business_date(trigger.occurred_at) if trigger is not None else \
            original["biz_date"]
        bound = self._bound.get(key)
        original["status"] = "reversed"
        claw = self._entry(
            event_seq=seq,
            biz_no=trigger.biz_no if trigger is not None else f"CLAW-{key}",
            ent=ent, store_id=loser_store or original["store_id"],
            biz_date=trigger_date, kind="clawback", amount=-original["amount"],
        )
        if bound:
            claw["status"] = "active"            # 进入下一账单抵扣
        else:
            claw["status"] = "reversed"          # 未出账，成对作废净额为 0
        claw["reverses"] = int(key)
        self.ledger[str(claw["entry_id"])] = claw

    # -- 退款 ----------------------------------------------------------------

    def _on_refund(self, event) -> None:
        ent = self.entitlements.get(event.entitlement_id)
        if ent is None:
            self._invalid(event, "退款了不存在的权益")
            return
        if not ent["winner_store"]:
            self._invalid(event, "权益未核销，不能退款")
            return
        if event.amount <= 0:
            self._invalid(event, "退款金额必须为正")
            return
        already = self._refund_face.get(event.entitlement_id, 0)
        if already + event.amount > ent["face_value"]:
            self._invalid(event, "累计退款超过权益面值")
            return
        rate = self.activities[(ent["activity_id"], ent["version"])]["rate"]
        subsidy = rate_mul(event.amount, rate)
        self._refund_face[event.entitlement_id] = already + event.amount
        ent["refunded_face"] = already + event.amount
        ent["refunded_amount"] += subsidy
        ent["status"] = "refunded" if ent["refunded_face"] == ent["face_value"] else "redeemed"
        entry = self._entry(
            event_seq=event.seq, biz_no=event.biz_no, ent=ent,
            store_id=ent["winner_store"],
            biz_date=business_date(event.occurred_at),
            kind="refund", amount=-subsidy,
        )
        self.ledger[str(entry["entry_id"])] = entry

    # -- 结账 ----------------------------------------------------------------

    def _on_settle(self, event) -> None:
        asked = event.payload.get("entry_ids", [])
        bound_ids: list = []
        amount = 0
        for entry_id in asked:
            entry = self.ledger.get(str(entry_id))
            if entry is None or entry["store_id"] != event.store_id:
                self._invalid(event, f"结账分录不存在或门店不符: {entry_id}")
                continue
            if entry["status"] != "active" or entry["bound"]:
                self._invalid(event, f"分录不可重复结账: {entry_id}")
                continue
            entry["bound"] = 1
            self._bound[str(entry_id)] = event.biz_no
            bound_ids.append(str(entry_id))
            amount += entry["amount"]
        self.settlements[event.biz_no] = {
            "biz_no": event.biz_no,
            "event_seq": event.seq,
            "store_id": event.store_id,
            "through_date": event.payload.get("through_date", ""),
            "amount": event.amount if event.amount else amount,
            "frozen_amount": 0,
            "entry_ids": bound_ids,
            "status": "open",
        }

    # -- 冻结/解冻 ------------------------------------------------------------

    def _on_unfreeze(self, event) -> None:
        ref = event.payload.get("ref", "")
        scope = event.payload.get("scope", "")
        hit = self.freezes.get(ref) if ref else None
        if ref and hit is None:
            self._invalid(event, f"解冻目标不存在: {ref}")
        if hit:
            hit["active"] = 0
            hit["unfreeze_biz_no"] = event.biz_no
        # 显式权益解冻（或解冻自动冻结）→ 关闭争议并释放该权益的全部冻结
        if scope == "entitlement" or event.entitlement_id:
            disp = self.disputes.get(event.entitlement_id)
            if disp:
                disp["status"] = "resolved"
            if event.entitlement_id:
                for f in self.freezes.values():
                    if (f["active"] and f["scope"] == "entitlement"
                            and f["entitlement_id"] == event.entitlement_id):
                        f["active"] = 0
                        f["unfreeze_biz_no"] = event.biz_no

    def _release_auto_freeze(self, entitlement_id: str) -> None:
        for f in self.freezes.values():
            if (f["active"] and f["scope"] == "entitlement"
                    and f["entitlement_id"] == entitlement_id
                    and f["reason"] == "争议自动冻结"):
                f["active"] = 0

    def _note_dispute(self, ent) -> None:
        lst = self._redeems[ent["entitlement_id"]]
        alive = [r for r in lst if not r["voided"]]
        stores = {r["store_id"] for r in alive}
        if len(stores) < 2:
            return
        winner = next(r for r in alive if r["seq"] == ent["redeem_seq"])
        # 败者取非胜者门店中最早的一笔
        loser = min((r for r in alive if r["store_id"] != winner["store_id"]),
                    key=lambda r: (r["occurred_at"], r["seq"]))
        # 已解决的争议可以被新一轮争抢重新打开
        self.disputes[ent["entitlement_id"]] = {
            "entitlement_id": ent["entitlement_id"],
            "winner_store": winner["store_id"],
            "loser_store": loser["store_id"],
            "winner_biz_no": winner["biz_no"],
            "loser_biz_no": loser["biz_no"],
            "loser_seq": loser["seq"],
            "status": "open",
        }

    # -- 汇总（重放结束后调用） -----------------------------------------------

    def freeze_covers(self, entry: dict) -> bool:
        if self.disputes.get(entry["entitlement_id"], {}).get("status") == "open":
            return True
        for f in self.freezes.values():
            if not f["active"]:
                continue
            if f["scope"] == "entitlement" and f["entitlement_id"] == entry["entitlement_id"]:
                return True
            if (f["scope"] == "store_date" and f["store_id"] == entry["store_id"]
                    and f["biz_date"] == entry["biz_date"]):
                return True
        return False

    def finalize(self) -> "Projection":
        # 败者核销 → rejected（后来补位成功的自然不在此列）
        for eid, lst in self._redeems.items():
            ent = self.entitlements[eid]
            for r in lst:
                if r["voided"] or r["seq"] == ent["redeem_seq"]:
                    continue
                self.rejected.append({
                    "event_seq": r["seq"], "biz_no": r["biz_no"],
                    "reason": "争抢失败：同一权益只生效一次",
                })
        # held 金额：只冻结门店实际应收的正向分录；
        # 冲销/退款等负向分录本就是追回，不参与冻结
        for bill in self.settlements.values():
            held = 0
            for entry_id in bill["entry_ids"]:
                entry = self.ledger.get(str(entry_id))
                if (entry and entry["amount"] > 0 and entry["status"] == "active"
                        and self.freeze_covers(entry)):
                    held += entry["amount"]
            bill["frozen_amount"] = held
        return self

    # -- 行序列化 -------------------------------------------------------------

    def _entry(self, event_seq, biz_no, ent, store_id, biz_date,
               kind, amount) -> dict:
        entry_id = self._alloc_id()
        entry = {
            "entry_id": entry_id,
            "event_seq": event_seq,
            "biz_no": biz_no,
            "entitlement_id": ent["entitlement_id"],
            "store_id": store_id,
            "community_id": self.activities[(ent["activity_id"], ent["version"])]["community_id"],
            "activity_id": ent["activity_id"],
            "version": ent["version"],
            "biz_date": biz_date,
            "kind": kind,
            "amount": amount,
            "status": "active",
            "bound": 0,
            "reverses": 0,
        }
        self.ledger[str(entry_id)] = entry
        return entry

    def _invalid(self, event, reason: str) -> None:
        self.invalid.append({"event_seq": event.seq, "biz_no": event.biz_no, "reason": reason})

    def ledger_rows(self) -> list[dict]:
        return [self.ledger[k] for k in sorted(self.ledger, key=int)]

    def settlement_rows(self) -> list[dict]:
        return [
            {**b, "entry_seqs": json_ids(b["entry_ids"])}
            for b in self.settlements.values()
        ]

    def freeze_rows(self) -> list[dict]:
        return list(self.freezes.values())

    def dispute_rows(self) -> list[dict]:
        return list(self.disputes.values())

    def rejected_rows(self) -> list[dict]:
        seen = {r["event_seq"] for r in self.rejected}
        rows = list(self.rejected)
        for r in self.invalid:
            if r["event_seq"] not in seen:
                rows.append(r)
        return rows


def json_ids(ids) -> str:
    return json.dumps(list(ids), ensure_ascii=False)
