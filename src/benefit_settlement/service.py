"""权益发行与清分应用服务。

每个对外命令：

1. 接受调用方（在线回调 / 离线补传）提供的 ``request_id`` 做幂等去重，
   重放同一请求返回同一业务编号与同一结果；
2. 在一个 ``BEGIN IMMEDIATE`` 事务内追加事件并整表重建投影后提交，
   进程在任何时刻崩溃都只会留下「旧投影 + 事件流」，重开后自动重放追平；
3. 业务时间 ``occurred_at`` 由终端上报，服务端不假设到达顺序，
   早发生晚补传的事件同样能纠正既有结果（冲销/补位/待抵扣）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .domain import (
    BIZ_PREFIX, FROZEN, ISSUED, PERIOD_SETTLED, REDEEMED, REFUNDED,
    UNFROZEN, VOIDED, ACTIVITY_PUBLISHED, Event, Record, business_date,
    cents, now_iso,
)
from .projection import Projection
from .store import DuplicateRequest, NaturalDuplicate, Store


def _to_cents(value) -> int:
    """数字（元）或整数分 → 整数分。int 透传，float/str/Decimal 按元换算。"""
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    if isinstance(value, int):
        return value
    return cents(value)


class Service:
    def __init__(self, store: Store | str | Path | None = None) -> None:
        self.store = store if isinstance(store, Store) else Store(store or ":memory:")
        self._recover_if_stale()

    def _recover_if_stale(self) -> None:
        """崩溃恢复：事件流比投影新（提交中断/外部破坏）时，启动即重放追平。"""
        conn = self.store.connection
        event_seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM event").fetchone()[0]
        row = conn.execute("SELECT value FROM proj_meta WHERE key='seq'").fetchone()
        proj_seq = int(row["value"]) if row else 0
        if event_seq != proj_seq:
            with self.store.tx() as tx_conn:
                self._rebuild(tx_conn)

    # ---------------------------------------------------------------- 健康

    def health(self) -> dict:
        return {"service": "benefit_settlement", "status": "ok"}

    # ------------------------------------------------------------ 活动版本

    def publish_activity(self, activity_id: str, version: int, community_id: str,
                         face_value, rate: str, effective_from: str,
                         request_id: str = "", occurred_at: str = "") -> dict:
        """发布活动版本。同一活动可有多个版本，按生效时间切换。"""
        payload = {"rate": str(rate), "effective_from": effective_from}
        return self._commit(
            ACTIVITY_PUBLISHED, request_id=request_id,
            occurred_at=occurred_at or f"{effective_from}T00:00:00+00:00",
            activity_id=activity_id, version=version,
            community_id=community_id, amount=_to_cents(face_value),
            payload=payload,
        )

    # ---------------------------------------------------------------- 发行

    def issue(self, entitlement_id: str, activity_id: str, community_id: str,
              request_id: str, occurred_at: str, version: int = 0,
              face_value=None) -> dict:
        """发行权益。version=0 表示按业务时间自动选择当时生效的活动版本。"""
        occurred_at = occurred_at or now_iso()
        with self.store.tx() as conn:
            if version == 0 or face_value is None:
                chosen = self._resolve_version(conn, activity_id, occurred_at)
                version = version or chosen["version"]
                face_value = face_value if face_value is not None else chosen["face_value"]
                community_id = community_id or chosen["community_id"]
            return self._commit(
                ISSUED, conn=conn, request_id=request_id, occurred_at=occurred_at,
                entitlement_id=entitlement_id, activity_id=activity_id,
                version=version, community_id=community_id,
                amount=_to_cents(face_value),
            )

    # ---------------------------------------------------------------- 核销

    def redeem(self, entitlement_id: str, store_id: str, request_id: str,
               occurred_at: str, community_id: str = "") -> dict:
        """门店核销（在线实时或离线补传同一入口）。

        核销导致同一权益出现两家未决门店时，同一事务内追加系统冻结事件，
        冻结该权益对应结算（可通过人工解冻解除）。
        """
        with self.store.tx() as conn:
            result = self._commit(
                REDEEMED, conn=conn, request_id=request_id,
                occurred_at=occurred_at, entitlement_id=entitlement_id,
                store_id=store_id, community_id=community_id,
            )
            if not result.get("duplicated"):
                self._maybe_auto_freeze(conn, entitlement_id)
            return result

    def _maybe_auto_freeze(self, conn, entitlement_id: str) -> None:
        dispute = conn.execute(
            "SELECT status FROM proj_dispute WHERE entitlement_id=?",
            (entitlement_id,),
        ).fetchone()
        if not dispute or dispute["status"] != "open":
            return
        active = conn.execute(
            "SELECT 1 FROM proj_freeze WHERE scope='entitlement' "
            "AND entitlement_id=? AND active=1",
            (entitlement_id,),
        ).fetchone()
        if active:
            return
        episode = conn.execute(
            "SELECT COUNT(*) FROM event WHERE type=? AND entitlement_id=?",
            (FROZEN, entitlement_id),
        ).fetchone()[0]
        suffix = "" if episode == 0 else f"-{episode + 1}"
        seq = self.store.next_seq(conn)
        event = Event(
            seq=seq, type=FROZEN, biz_no=f"FRZ-AUTO-{entitlement_id}{suffix}",
            occurred_at=now_iso(), recorded_at=now_iso(),
            entitlement_id=entitlement_id, request_id=None,
            payload={"scope": "entitlement", "reason": "争议自动冻结"},
        )
        self.store.append_event(conn, event)
        self._rebuild(conn)

    # ---------------------------------------------------------------- 撤销

    def void_redeem(self, entitlement_id: str, store_id: str, ref: str,
                    request_id: str, occurred_at: str) -> dict:
        """撤销核销。ref 为原核销的业务编号或 request_id，只能撤销本店核销。"""
        return self._commit(
            VOIDED, request_id=request_id, occurred_at=occurred_at,
            entitlement_id=entitlement_id, store_id=store_id,
            payload={"ref": ref},
        )

    # ---------------------------------------------------------------- 退款

    def refund(self, entitlement_id: str, amount, request_id: str,
               occurred_at: str, store_id: str = "") -> dict:
        """退款（支持部分退款，金额按面值计）。晚于结账自动落入下一账单抵扣。"""
        return self._commit(
            REFUNDED, request_id=request_id, occurred_at=occurred_at,
            entitlement_id=entitlement_id, amount=_to_cents(amount),
            store_id=store_id,
        )

    # ---------------------------------------------------------------- 结账

    def close_period(self, store_id: str, through_date: str, request_id: str,
                     entry_ids: list[str] | None = None,
                     occurred_at: str = "") -> dict:
        """门店按业务日结账；不传 entry_ids 时自动收齐截至日的全部待结账分录。

        显式传入的分录若不存在/已结账/门店不符，投影会逐条记 warning 并跳过，
        合法分录照常结账；账单金额以实际绑定的分录合计为准。
        """
        with self.store.tx() as conn:
            if entry_ids is None:
                entry_ids = self._pending_entry_ids(conn, store_id, through_date)
            return self._commit(
                PERIOD_SETTLED, conn=conn, request_id=request_id,
                occurred_at=occurred_at or f"{through_date}T23:59:59+00:00",
                store_id=store_id, amount=0,
                payload={"through_date": through_date, "entry_ids": entry_ids},
            )

    # ------------------------------------------------------------ 冻结解冻

    def freeze(self, scope: str, reason: str, request_id: str,
               occurred_at: str = "", entitlement_id: str = "",
               store_id: str = "", biz_date: str = "") -> dict:
        """争议冻结。scope='entitlement' 只冻单笔；scope='store_date' 冻门店某日。

        同一权益被多家门店核销时，系统在第二次核销落库时自动产生 entitlement 冻结。
        """
        if scope not in ("entitlement", "store_date"):
            raise ValueError("scope 只能是 entitlement 或 store_date")
        if scope == "entitlement" and not entitlement_id:
            raise ValueError("entitlement 冻结必须指定权益")
        if scope == "store_date" and not (store_id and biz_date):
            raise ValueError("store_date 冻结必须指定门店和业务日")
        return self._commit(
            FROZEN, request_id=request_id, occurred_at=occurred_at or now_iso(),
            entitlement_id=entitlement_id, store_id=store_id,
            payload={"scope": scope, "reason": reason, "biz_date": biz_date},
        )

    def unfreeze(self, ref: str, request_id: str, operator: str,
                 occurred_at: str = "", resolution: str = "manual",
                 entitlement_id: str = "") -> dict:
        """人工解冻：ref 为冻结业务编号；争议解冻同时关闭争议。"""
        return self._commit(
            UNFROZEN, request_id=request_id, occurred_at=occurred_at or now_iso(),
            entitlement_id=entitlement_id,
            payload={"ref": ref, "operator": operator, "resolution": resolution},
        )

    # ------------------------------------------------------------ 查询对账

    def entitlement(self, entitlement_id: str) -> dict | None:
        row = self.store.connection.execute(
            "SELECT * FROM proj_entitlement WHERE entitlement_id=?",
            (entitlement_id,),
        ).fetchone()
        return dict(row) if row else None

    def store_balance(self, store_id: str, through_date: str | None = None) -> dict:
        """门店在途余额：按业务日汇总全部生效分录（含未结账与已结账）。"""
        sql = ("SELECT biz_date, kind, status, amount FROM proj_ledger "
               "WHERE store_id=?")
        args: list = [store_id]
        if through_date:
            sql += " AND biz_date<=?"
            args.append(through_date)
        rows = self.store.connection.execute(sql, args).fetchall()
        active = sum(r["amount"] for r in rows if r["status"] == "active")
        bills = self.store.connection.execute(
            "SELECT COALESCE(SUM(amount),0) settled, "
            "COALESCE(SUM(frozen_amount),0) frozen FROM proj_settlement WHERE store_id=?",
            (store_id,),
        ).fetchone()
        return {
            "store_id": store_id,
            "through_date": through_date or "",
            "active_amount": active,
            "settled_amount": bills["settled"],
            "frozen_amount": bills["frozen"],
            "available_amount": bills["settled"] - bills["frozen"],
        }

    def bill(self, biz_no: str) -> dict | None:
        row = self.store.connection.execute(
            "SELECT * FROM proj_settlement WHERE biz_no=?", (biz_no,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["entries"] = json.loads(result.pop("entry_seqs"))
        return result

    def store_bills(self, store_id: str) -> list[dict]:
        rows = self.store.connection.execute(
            "SELECT * FROM proj_settlement WHERE store_id=? ORDER BY event_seq",
            (store_id,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["entries"] = json.loads(item.pop("entry_seqs"))
            out.append(item)
        return out

    def ledger(self, store_id: str = "", entitlement_id: str = "") -> list[dict]:
        sql = "SELECT * FROM proj_ledger WHERE 1=1"
        args: list = []
        if store_id:
            sql += " AND store_id=?"
            args.append(store_id)
        if entitlement_id:
            sql += " AND entitlement_id=?"
            args.append(entitlement_id)
        sql += " ORDER BY event_seq, entry_id"
        return [dict(r) for r in self.store.connection.execute(sql, args).fetchall()]

    def disputes(self, status: str = "") -> list[dict]:
        sql = "SELECT * FROM proj_dispute"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        return [dict(r) for r in self.store.connection.execute(sql, args).fetchall()]

    def rejected(self) -> list[dict]:
        return [dict(r) for r in
                self.store.connection.execute("SELECT * FROM proj_rejected ORDER BY event_seq")]

    def reconcile(self) -> dict:
        """对账：事件流重算结果与已持久化投影逐表比对。"""
        projected = self._replay()
        conn = self.store.connection
        problems: list[str] = []

        def compare(name: str, stored_rows, fresh_rows, key: str):
            stored = {r[key]: r for r in stored_rows}
            fresh = {r[key]: r for r in fresh_rows}
            if set(stored) != set(fresh):
                problems.append(f"{name}: 主键集合不一致")
                return
            for k, fresh_row in fresh.items():
                for col, value in fresh_row.items():
                    if col == "entry_ids":
                        continue
                    stored_val = stored[k].get(col)
                    if str(stored_val) != str(value):
                        problems.append(f"{name}[{k}].{col}: 存储={stored_val!r} 重算={value!r}")

        compare("ledger",
                [dict(r) for r in conn.execute("SELECT * FROM proj_ledger")],
                projected.ledger_rows(), "entry_id")
        compare("settlement",
                [dict(r, entry_seqs=None, entry_ids=None)
                 for r in conn.execute("SELECT * FROM proj_settlement")],
                [{**r, "entry_seqs": None, "entry_ids": None}
                 for r in projected.settlement_rows()],
                "biz_no")
        compare("entitlement",
                [dict(r) for r in conn.execute("SELECT * FROM proj_entitlement")],
                list(projected.entitlements.values()), "entitlement_id")
        compare("freeze",
                [dict(r) for r in conn.execute("SELECT * FROM proj_freeze")],
                projected.freeze_rows(), "biz_no")
        compare("dispute",
                [dict(r) for r in conn.execute("SELECT * FROM proj_dispute")],
                projected.dispute_rows(), "entitlement_id")
        compare("rejected",
                [dict(r) for r in conn.execute("SELECT * FROM proj_rejected")],
                projected.rejected_rows(), "event_seq")
        stored_act = [dict(r) for r in conn.execute(
            "SELECT * FROM proj_activity ORDER BY activity_id, version")]
        fresh_act = [projected.activities[k]
                     for k in sorted(projected.activities)]
        if len(stored_act) != len(fresh_act) or any(
                stored_act[i] != fresh_act[i] for i in range(len(fresh_act))):
            problems.append("activity: 活动版本投影不一致")

        # 勾稽 1：每张账单金额 = 其绑定分录金额之和（分录金额不可变）
        bound_ids: set[str] = set()
        for bill in projected.settlements.values():
            total = 0
            for entry_id in bill["entry_ids"]:
                entry = projected.ledger.get(str(entry_id))
                if entry is None:
                    problems.append(f"账单 {bill['biz_no']} 引用了不存在的分录 {entry_id}")
                    continue
                total += entry["amount"]
                bound_ids.add(str(entry_id))
            if total != bill["amount"]:
                problems.append(
                    f"账单 {bill['biz_no']}: 金额={bill['amount']} 分录合计={total}")
        # 勾稽 2（交叉校验，不经过账单金额字段）：
        # 账单总额恒等于「全部已绑定分录」金额之和——绑定是一次性快照，
        # 即使分录事后被冲销，也由另一张账单上的负向分录配平。
        settled_total = sum(b["amount"] for b in projected.settlements.values())
        bound_total = sum(projected.ledger[i]["amount"] for i in bound_ids)
        if settled_total != bound_total:
            problems.append(
                f"勾稽不成立: 账单总额={settled_total} 已绑定分录合计={bound_total}")
        # 勾稽 3：被冲销的已绑定正向分录，必有等额冲销分录精确配平
        for i in bound_ids:
            entry = projected.ledger[i]
            if entry["status"] == "reversed" and entry["kind"] == "settle":
                claw = sum(
                    e["amount"] for e in projected.ledger.values()
                    if e.get("reverses") == int(i)
                )
                if claw != -entry["amount"]:
                    problems.append(
                        f"冲销不配平: 分录 {i} 金额 {entry['amount']}，"
                        f"对应 clawback 合计 {claw}")
        active_sum = sum(e["amount"] for e in projected.ledger.values()
                         if e["status"] == "active")
        return {"ok": not problems, "problems": problems,
                "events": projected.seq, "active_amount": active_sum}

    def recompute(self) -> dict:
        """手工重算：清空投影并用事件流重建（对账发现漂移时使用）。"""
        with self.store.tx() as conn:
            count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            self._rebuild(conn)
        return {"ok": True, "replayed_events": count}

    # ------------------------------------------------------------ 旧版能力

    def register(self, record_id: str, owner_id: str) -> dict:
        with self.store.tx() as conn:
            record = self.store.save_record(conn, Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict | None:
        record = self.store.get_record(record_id)
        return record.__dict__.copy() if record else None

    # ================================================================ 内部

    def _commit(self, event_type: str, *, request_id: str, occurred_at: str,
                entitlement_id: str = "", activity_id: str = "", version: int = 0,
                community_id: str = "", store_id: str = "", amount: int = 0,
                payload: dict | None = None, conn=None) -> dict:
        """幂等命令的统一提交流程。

        每次调用恰好产生一条事件（重放返回首次结果）；事件追加与投影重建
        在同一事务内原子提交。
        """
        request_id = str(request_id or "")
        if not request_id:
            raise ValueError("request_id 不能为空：每笔命令必须可重放")
        if conn is not None:
            return self._commit_locked(
                conn, event_type, request_id, occurred_at, entitlement_id,
                activity_id, version, community_id, store_id, amount, payload,
            )
        with self.store.tx() as conn:
            return self._commit_locked(
                conn, event_type, request_id, occurred_at, entitlement_id,
                activity_id, version, community_id, store_id, amount, payload,
            )

    def _commit_locked(self, conn, event_type, request_id, occurred_at,
                       entitlement_id, activity_id, version, community_id,
                       store_id, amount, payload) -> dict:
        cached = conn.execute(
            "SELECT result FROM inbox WHERE request_id=?", (request_id,)
        ).fetchone()
        if cached:
            result = json.loads(cached["result"])
            result["duplicated"] = True
            return result

        seq = self.store.next_seq(conn)
        biz_no = f"{BIZ_PREFIX[event_type]}-{seq:010d}"
        event = Event(
            seq=seq, type=event_type, biz_no=biz_no,
            occurred_at=occurred_at or now_iso(), recorded_at=now_iso(),
            request_id=request_id, entitlement_id=entitlement_id,
            activity_id=activity_id, version=version,
            community_id=community_id, store_id=store_id, amount=amount,
            payload=payload or {},
        )
        try:
            self.store.append_event(conn, event)
        except DuplicateRequest as exc:
            result = dict(exc.result)
            result["duplicated"] = True
            return result
        except NaturalDuplicate as exc:
            old = exc.event
            result = {
                "biz_no": old.biz_no, "type": old.type, "duplicated": True,
                "dedup": "natural_key",
                "entitlement_id": old.entitlement_id,
                "store_id": old.store_id,
            }
            # 自然键没有 inbox 记录，补记一条，保证同一 request_id 以后也命中
            self.store.remember(conn, request_id, old.biz_no,
                                {k: v for k, v in result.items() if k != "duplicated"})
            return result

        projection = self._rebuild(conn)
        warnings = [{"event_seq": i["event_seq"], "biz_no": i["biz_no"],
                     "reason": i["reason"]}
                    for i in projection.invalid if i["event_seq"] == seq]
        result: dict = {
            "biz_no": biz_no, "type": event_type, "seq": seq,
            "occurred_at": event.occurred_at, "duplicated": False,
        }
        if entitlement_id:
            result["entitlement_id"] = entitlement_id
        if store_id:
            ent = projection.entitlements.get(entitlement_id)
            result["store_id"] = store_id
            result["settle_amount"] = ent["settle_amount"] if ent else 0
        if event_type == PERIOD_SETTLED:
            bill = projection.settlements[biz_no]
            result["amount"] = bill["amount"]
            result["frozen_amount"] = bill["frozen_amount"]
            result["entries"] = bill["entry_ids"]
        if warnings:
            result["warnings"] = warnings
        self.store.remember(conn, request_id, biz_no,
                            {k: v for k, v in result.items() if k != "duplicated"})
        return result

    def _rebuild(self, conn) -> Projection:
        projection = Projection()
        events = [Event.from_row(r)
                  for r in conn.execute("SELECT * FROM event ORDER BY seq")]
        projection.apply_all(events).finalize()
        self.store.reset_projection(conn)
        self.store.load_projection(projection)
        return projection

    def _replay(self) -> Projection:
        return Projection().apply_all(self.store.list_events()).finalize()

    def _resolve_version(self, conn, activity_id: str, occurred_at: str) -> dict:
        rows = conn.execute(
            "SELECT * FROM proj_activity WHERE activity_id=? ORDER BY version",
            (activity_id,),
        ).fetchall()
        if not rows:
            raise ValueError(f"活动未发布: {activity_id}")
        biz_date = business_date(occurred_at)
        candidates = [dict(r) for r in rows if r["effective_from"] <= biz_date]
        if not candidates:
            raise ValueError(
                f"发行时间 {biz_date} 早于活动 {activity_id} 最早版本生效日")
        return max(candidates, key=lambda r: r["version"])

    def _pending_entry_ids(self, conn, store_id: str, through_date: str) -> list[str]:
        rows = conn.execute(
            """SELECT entry_id, biz_date FROM proj_ledger
               WHERE store_id=? AND status='active' AND bound=0 AND biz_date<=?
               ORDER BY entry_id""",
            (store_id, through_date),
        ).fetchall()
        return [str(r["entry_id"]) for r in rows]

    def close(self) -> None:
        self.store.close()
