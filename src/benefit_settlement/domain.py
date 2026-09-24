"""权益事件与门店清分的基础领域对象。"""
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or datetime.now(timezone.utc).isoformat())
