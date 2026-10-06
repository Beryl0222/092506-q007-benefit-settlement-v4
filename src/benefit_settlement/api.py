"""进程内 JSON 请求适配层。

所有写动作都必须携带 ``request_id``（终端/渠道生成的唯一号），
服务端据此保证重放安全；``occurred_at`` 为业务发生时间，离线补传时如实回填。

金额字段（``face_value`` / ``amount``）可用数字元（如 10.5）或整数分（int）。
"""
from __future__ import annotations

import json

from .service import Service


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    action = body.get("action")
    data = body.get("data", body)   # 允许 {"action": ..., "data": {...}} 或扁平结构

    if action == "health":
        # 健康检查保持扁平响应，兼容基线探针
        return json.dumps(service.health(), ensure_ascii=False)
    if action == "register":
        return _ok(service.register(str(data["record_id"]), str(data["owner_id"])))
    if action == "publish_activity":
        return _ok(service.publish_activity(
            data["activity_id"], int(data["version"]), data["community_id"],
            data["face_value"], str(data["rate"]), data["effective_from"],
            request_id=data["request_id"],
            occurred_at=data.get("occurred_at", "")))
    if action == "issue":
        return _ok(service.issue(
            data["entitlement_id"], data["activity_id"],
            data.get("community_id", ""), data["request_id"],
            data["occurred_at"], version=int(data.get("version", 0)),
            face_value=data.get("face_value")))
    if action == "redeem":
        return _ok(service.redeem(
            data["entitlement_id"], data["store_id"], data["request_id"],
            data["occurred_at"], data.get("community_id", "")))
    if action == "void":
        return _ok(service.void_redeem(
            data["entitlement_id"], data["store_id"], data["ref"],
            data["request_id"], data["occurred_at"]))
    if action == "refund":
        return _ok(service.refund(
            data["entitlement_id"], data["amount"], data["request_id"],
            data["occurred_at"], data.get("store_id", "")))
    if action == "close_period":
        return _ok(service.close_period(
            data["store_id"], data["through_date"], data["request_id"],
            entry_ids=data.get("entry_ids"),
            occurred_at=data.get("occurred_at", "")))
    if action == "freeze":
        return _ok(service.freeze(
            data["scope"], data["reason"], data["request_id"],
            occurred_at=data.get("occurred_at", ""),
            entitlement_id=data.get("entitlement_id", ""),
            store_id=data.get("store_id", ""),
            biz_date=data.get("biz_date", "")))
    if action == "unfreeze":
        return _ok(service.unfreeze(
            data["ref"], data["request_id"], data["operator"],
            occurred_at=data.get("occurred_at", ""),
            resolution=data.get("resolution", "manual"),
            entitlement_id=data.get("entitlement_id", "")))
    if action == "entitlement":
        return _ok(service.entitlement(data["entitlement_id"]))
    if action == "balance":
        return _ok(service.store_balance(
            data["store_id"], data.get("through_date")))
    if action == "bill":
        return _ok(service.bill(data["biz_no"]))
    if action == "bills":
        return _ok(service.store_bills(data["store_id"]))
    if action == "ledger":
        return _ok(service.ledger(
            data.get("store_id", ""), data.get("entitlement_id", "")))
    if action == "disputes":
        return _ok(service.disputes(data.get("status", "")))
    if action == "rejected":
        return _ok(service.rejected())
    if action == "reconcile":
        return _ok(service.reconcile())
    if action == "recompute":
        return _ok(service.recompute())
    raise ValueError(f"不支持的请求动作: {action}")


def _ok(result) -> str:
    return json.dumps({"ok": True, "data": result}, ensure_ascii=False)
