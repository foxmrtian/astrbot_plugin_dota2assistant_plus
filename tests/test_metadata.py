"""插件元数据（metadata.yaml / pyproject.toml）的完整性校验。

AstrBot 加载插件时会校验 `metadata.yaml`，字段类型不对会**静默退回默认元数据**
（日志只有一行 WARN，插件照常工作），因此版本号写错很难被察觉。
真实事故：版本号写成 `0.26` 时 YAML 解析成**浮点数**，AstrBot 报
「字段 version 必须是非空字符串」并弃用整份元数据。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_METADATA = _ROOT / "metadata.yaml"
_PYPROJECT = _ROOT / "pyproject.toml"


class TestMetadata(unittest.TestCase):
    def test_metadata_version_is_a_string(self):
        """version 必须是字符串 —— `0.26` 会被 YAML 当成浮点数。"""
        data = yaml.safe_load(_METADATA.read_text(encoding="utf-8"))
        self.assertIn("version", data)
        self.assertIsInstance(
            data["version"], str,
            f"metadata.yaml 的 version 被解析成 {type(data['version']).__name__}，"
            "AstrBot 会因「必须是非空字符串」弃用整份元数据；"
            "请写成 version: \"0.26\" 这种带引号的形式",
        )
        self.assertTrue(data["version"].strip())

    def test_all_required_string_fields_are_strings(self):
        data = yaml.safe_load(_METADATA.read_text(encoding="utf-8"))
        for field in ("name", "desc", "short_desc", "version", "author"):
            with self.subTest(field=field):
                self.assertIsInstance(data.get(field), str, f"{field} 必须是字符串")
                self.assertTrue(str(data[field]).strip(), f"{field} 不能为空")

    def test_versions_match_between_metadata_and_pyproject(self):
        """两处版本号必须一致，否则发布产物与插件自述会互相矛盾。"""
        meta = yaml.safe_load(_METADATA.read_text(encoding="utf-8"))
        text = _PYPROJECT.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        self.assertIsNotNone(match, "pyproject.toml 里没找到 version")
        self.assertEqual(meta["version"], match.group(1))

    def test_changelog_documents_current_version(self):
        """CHANGELOG 必须有一条与当前版本号对应的记录。"""
        meta = yaml.safe_load(_METADATA.read_text(encoding="utf-8"))
        changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{meta['version']}]", changelog)


if __name__ == "__main__":
    unittest.main()
