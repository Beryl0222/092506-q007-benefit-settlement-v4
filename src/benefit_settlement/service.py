"""权益事件与门店清分的应用服务入口。"""
from .store import Store
from .domain import Record


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    def health(self) -> dict[str, str]:
        return {"service": "benefit_settlement", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                 "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None
