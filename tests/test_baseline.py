import json
import unittest
from benefit_settlement.api import handle
from benefit_settlement.service import Service
from benefit_settlement.store import Store


class 基线行为测试(unittest.TestCase):
    def test_health(self):
        result = json.loads(handle(json.dumps({"action": "health"}), Service(Store())))
        self.assertEqual(result["status"], "ok")

    def test_register_and_find(self):
        service = Service(Store())
        created = service.register("r-1", "owner-1")
        self.assertEqual(created["state"], "draft")
        self.assertEqual(service.find("r-1")["owner_id"], "owner-1")


if __name__ == "__main__":
    unittest.main()
