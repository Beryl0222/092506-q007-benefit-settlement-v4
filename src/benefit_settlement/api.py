"""供进程内调用的轻量请求适配层。"""
import json
from .service import Service


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    if body.get("action") == "health":
        return json.dumps(service.health(), ensure_ascii=False)
    if body.get("action") == "register":
        return json.dumps(service.register(str(body["record_id"]), str(body["owner_id"])), ensure_ascii=False)
    raise ValueError("不支持的请求动作")
