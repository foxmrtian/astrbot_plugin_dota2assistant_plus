"""数据持久化测试：验证 SQLite 数据在重新打开后仍然存在。"""

import unittest
from pathlib import Path
from uuid import uuid4

from astrbot_plugin_dota2assistant_plus.core.store import DotaStore


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / "test_tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.temp_root / f"persist-{uuid4().hex}.db"

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_bind_survives_reopen(self):
        """绑定数据在重新打开数据库后仍然存在。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_001", 899428504, "miracle")

        store2 = DotaStore(self.db_path)
        result = store2.get_bound_account("user_001")
        self.assertEqual(result, 899428504)

    def test_bind_info_survives_reopen(self):
        """绑定信息（含 persona_name）在重新打开后仍然完整。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_002", 123456789, "TestPlayer")

        store2 = DotaStore(self.db_path)
        info = store2.get_binding_info("user_002")
        self.assertIsNotNone(info)
        self.assertEqual(info[0], 123456789)
        self.assertEqual(info[1], "TestPlayer")

    def test_unbind_survives_reopen(self):
        """解绑操作在重新打开后生效。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_003", 111, "Player")
        store1.unbind_account("user_003")

        store2 = DotaStore(self.db_path)
        self.assertIsNone(store2.get_bound_account("user_003"))

    def test_update_survives_reopen(self):
        """更新绑定（相同 sender_id）在重新打开后保留最新值。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_004", 111, "Old")
        store1.bind_account("user_004", 222, "New")

        store2 = DotaStore(self.db_path)
        info = store2.get_binding_info("user_004")
        self.assertIsNotNone(info)
        self.assertEqual(info[0], 222)
        self.assertEqual(info[1], "New")

    def test_multiple_users_persist(self):
        """多个用户绑定在重新打开后全部存在。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("u1", 100, "A")
        store1.bind_account("u2", 200, "B")
        store1.bind_account("u3", 300, "C")

        store2 = DotaStore(self.db_path)
        self.assertEqual(store2.get_bound_account("u1"), 100)
        self.assertEqual(store2.get_bound_account("u2"), 200)
        self.assertEqual(store2.get_bound_account("u3"), 300)

    def test_partial_unbind_persist(self):
        """部分解绑后重新打开，剩余绑定不受影响。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("u1", 100, "A")
        store1.bind_account("u2", 200, "B")
        store1.unbind_account("u1")

        store2 = DotaStore(self.db_path)
        self.assertIsNone(store2.get_bound_account("u1"))
        self.assertEqual(store2.get_bound_account("u2"), 200)

    def test_schema_upgrade_preserves_data(self):
        """Schema 迁移（新增列）不丢失已有数据。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_005", 999, "OldPlayer")

        # 再次打开会触发 _init_db（含 CREATE TABLE IF NOT EXISTS）
        store2 = DotaStore(self.db_path)
        store2.bind_account("user_006", 888, "NewPlayer")

        # 两个用户都应该存在
        self.assertEqual(store2.get_bound_account("user_005"), 999)
        self.assertEqual(store2.get_bound_account("user_006"), 888)

    def test_empty_db_reopen(self):
        """空数据库重新打开后查询不报错。"""
        DotaStore(self.db_path)
        store2 = DotaStore(self.db_path)
        self.assertIsNone(store2.get_bound_account("nobody"))

    def test_chinese_persona_name(self):
        """中文 persona_name 正确持久化。"""
        store1 = DotaStore(self.db_path)
        store1.bind_account("user_cn", 123, "测试玩家")

        store2 = DotaStore(self.db_path)
        info = store2.get_binding_info("user_cn")
        self.assertIsNotNone(info)
        self.assertEqual(info[1], "测试玩家")

    def test_special_char_sender_id(self):
        """sender_id 含特殊字符（如平台前缀）时正确持久化。"""
        sender = "aiocqhttp:group:123456:789"
        store1 = DotaStore(self.db_path)
        store1.bind_account(sender, 456, "Player")

        store2 = DotaStore(self.db_path)
        self.assertEqual(store2.get_bound_account(sender), 456)


if __name__ == "__main__":
    unittest.main()
