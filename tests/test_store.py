import unittest
from pathlib import Path
from uuid import uuid4

from astrbot_plugin_dota2assistant_plus.core.store import DotaStore


class TestDotaStore(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / "test_tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_root / f"test-{uuid4().hex}.db"
        self.store = DotaStore(self.db_path)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_bind_and_get(self):
        self.store.bind_account("user1", 12345, "TestPlayer")
        account_id = self.store.get_bound_account("user1")
        self.assertEqual(account_id, 12345)

    def test_bind_update(self):
        self.store.bind_account("user1", 12345, "OldName")
        self.store.bind_account("user1", 67890, "NewName")
        account_id = self.store.get_bound_account("user1")
        self.assertEqual(account_id, 67890)

    def test_get_binding_info(self):
        self.store.bind_account("user1", 12345, "TestPlayer")
        info = self.store.get_binding_info("user1")
        self.assertIsNotNone(info)
        self.assertEqual(info[0], 12345)
        self.assertEqual(info[1], "TestPlayer")

    def test_unbind(self):
        self.store.bind_account("user1", 12345, "TestPlayer")
        result = self.store.unbind_account("user1")
        self.assertTrue(result)
        self.assertIsNone(self.store.get_bound_account("user1"))

    def test_unbind_nonexistent(self):
        result = self.store.unbind_account("nobody")
        self.assertFalse(result)

    def test_get_nonexistent(self):
        self.assertIsNone(self.store.get_bound_account("nobody"))

    def test_multiple_users(self):
        self.store.bind_account("user1", 111, "Player1")
        self.store.bind_account("user2", 222, "Player2")
        self.assertEqual(self.store.get_bound_account("user1"), 111)
        self.assertEqual(self.store.get_bound_account("user2"), 222)

    def test_schema_migration_preserves_data(self):
        self.store.bind_account("user1", 12345, "TestPlayer")
        # Re-open store (triggers _init_db again)
        store2 = DotaStore(self.db_path)
        account_id = store2.get_bound_account("user1")
        self.assertEqual(account_id, 12345)


if __name__ == "__main__":
    unittest.main()
