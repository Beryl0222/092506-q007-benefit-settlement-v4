"""权益发行、核销、撤销、退款与清分的领域模型。

约定：
- 金额一律使用整数「分」，杜绝浮点误差；
- 业务时间 ``occurred_at`` 表示事件在线上/线下终端真实发生的时间，
  记录时间 ``recorded_at`` 表示服务端落库时间，跨日补传时两者可能相差数天；
- 所有事件都是不可变事实，清分结果由事件流重放得到（事件溯源）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
# ---- 事件类型 -----------------------------------------------------------

ACTIVITY_PUBLISHED = "activity_published"   # 活动版本发布
ISSUED = "issued"                           # 权益发行
REDEEMED = "redeemed"                       # 核销
VOIDED = "voided"                           # 核销撤销
REFUNDED = "refunded"                       # 退款（可部分）
PERIOD_SETTLED = "period_settled"           # 门店账单日结账
FROZEN = "frozen"                           # 冻结结算
UNFROZEN = "unfrozen"                       # 人工解冻

MUTABLE_TYPES = (ISSUED, REDEEMED, VOIDED, REFUNDED)

# 业务编号前缀
BIZ_PREFIX = {
    ACTIVITY_PUBLISHED: "ACT",
    ISSUED: "ISS",
    REDEEMED: "RED",
    VOIDED: "VOI",
    REFUNDED: "REF",
    PERIOD_SETTLED: "SET",
    FROZEN: "FRZ",
    UNFROZEN: "UFR",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def business_date(occurred_at: str) -> str:
    """取业务时间对应的 UTC 日期（YYYY-MM-DD），账单按业务日归集。"""
    return occurred_at[:10]


def cents(value: Decimal | float | str) -> int:
    """元转分，四舍五入到分。"""
    return int(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def rate_mul(face_value: int, rate: str) -> int:
    """活动版本补贴费率 × 面值，四舍五入到分。"""
    amount = (Decimal(face_value) * Decimal(rate)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(amount)


@dataclass(frozen=True)
class Record:
    """旧版基础登记记录，保留以兼容基线能力。"""
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or now_iso())


@dataclass(frozen=True)
class Event:
    """一条不可变业务事件。

    ``seq`` 是服务端落库顺序；乱序到达时业务判定仍以 ``occurred_at`` 为准，
    因此重放结果与消息到达顺序无关。
    """
    seq: int
    type: str
    biz_no: str
    occurred_at: str
    recorded_at: str
    entitlement_id: str = ""
    activity_id: str = ""
    version: int = 0
    community_id: str = ""
    store_id: str = ""
    amount: int = 0
    request_id: str | None = ""
    payload: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        row = {
            "seq": self.seq,
            "biz_no": self.biz_no,
            "request_id": self.request_id,
            "type": self.type,
            "entitlement_id": self.entitlement_id,
            "activity_id": self.activity_id,
            "version": self.version,
            "community_id": self.community_id,
            "store_id": self.store_id,
            "amount": self.amount,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "natural_key": self.natural_key(),
            "payload": json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
        }
        return row

    @classmethod
    def from_row(cls, row) -> "Event":
        return cls(
            seq=row["seq"],
            type=row["type"],
            biz_no=row["biz_no"],
            occurred_at=row["occurred_at"],
            recorded_at=row["recorded_at"],
            entitlement_id=row["entitlement_id"] or "",
            activity_id=row["activity_id"] or "",
            version=row["version"] or 0,
            community_id=row["community_id"] or "",
            store_id=row["store_id"] or "",
            amount=row["amount"] or 0,
            request_id=row["request_id"] or "",
            payload=json.loads(row["payload"] or "{}"),
        )

    def natural_key(self) -> str:
        """同一笔线下业务的「自然指纹」。

        即使终端没有生成 request_id，补传同一条核销/退款也只会生效一次；
        payload 整体纳入指纹（如同版本费率不同则视为不同事实）。
        """
        return "|".join([
            self.type,
            self.entitlement_id,
            self.store_id,
            self.community_id,
            self.activity_id,
            str(self.version),
            str(self.amount),
            self.occurred_at,
            json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
        ])
