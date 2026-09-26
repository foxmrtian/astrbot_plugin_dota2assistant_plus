import unittest

from astrbot_plugin_dota2assistant_plus.core.opendota import (
    OpenDotaClient,
    OpenDotaRequestError,
    _extract_item_description,
    _is_radiant_win,
)


class TestHelpers(unittest.TestCase):
    def test_is_radiant_win_radiant_player_wins(self):
        data = {"radiant_win": True, "player_slot": 1}
        self.assertTrue(_is_radiant_win(data))

    def test_is_radiant_win_radiant_player_loses(self):
        data = {"radiant_win": False, "player_slot": 1}
        self.assertFalse(_is_radiant_win(data))

    def test_is_radiant_win_dire_player_wins(self):
        data = {"radiant_win": False, "player_slot": 128}
        self.assertTrue(_is_radiant_win(data))

    def test_is_radiant_win_dire_player_loses(self):
        data = {"radiant_win": True, "player_slot": 128}
        self.assertFalse(_is_radiant_win(data))

    def test_extract_item_description_with_abilities(self):
        item = {
            "abilities": [{"description": "Active: Blink"}, {"description": "Cooldown: 15s"}],
            "hint": ["Some hint"],
        }
        result = _extract_item_description(item)
        self.assertIn("Blink", result)
        self.assertIn("Cooldown", result)
        self.assertIn("hint", result)

    def test_extract_item_description_empty(self):
        result = _extract_item_description({})
        self.assertEqual(result, "")

    def test_extract_item_description_truncation(self):
        item = {"abilities": [{"description": "x" * 500}], "hint": []}
        result = _extract_item_description(item)
        self.assertLessEqual(len(result), 300)


class TestRequestFailureIsNotMisreportedAsMissingData(unittest.TestCase):
    """请求失败不能被说成「数据不存在」。

    线上曾出现：OpenDota 请求超时（日志 `玩家搜索 请求超时。`）时
    `_get` 返回 None，`search_players` 把它和"空结果"一起变成 []，
    工具于是回「找不到名为 'Ame' 的玩家」——用户以为选手查不到，
    实际只是网络抖动，而且因为没有卡片数据，连图片也出不来。
    """

    def _client_with(self, *, raises=None, returns=None):
        client = OpenDotaClient(timeout=1)

        async def fake_get(path, params=None, context="OpenDota"):
            if raises is not None:
                raise raises
            return returns

        client._get = fake_get  # type: ignore[assignment]
        return client

    def test_search_players_propagates_request_failure(self):
        import asyncio

        client = self._client_with(raises=OpenDotaRequestError("玩家搜索 请求超时。"))
        with self.assertRaises(OpenDotaRequestError):
            asyncio.run(client.search_players("Ame"))

    def test_search_players_empty_result_stays_empty(self):
        import asyncio

        client = self._client_with(returns=[])
        self.assertEqual(asyncio.run(client.search_players("Ame")), [])

    def test_match_detail_propagates_request_failure(self):
        import asyncio

        client = self._client_with(raises=OpenDotaRequestError("比赛详情 请求超时。"))
        with self.assertRaises(OpenDotaRequestError):
            asyncio.run(client.get_match_detail(8987081176))

    def test_match_detail_falls_back_to_valve_when_configured(self):
        import asyncio

        class FakeValve:
            called = False

            async def get_match_detail(self, match_id):
                FakeValve.called = True
                return f"valve-{match_id}"

        client = self._client_with(raises=OpenDotaRequestError("比赛详情 请求超时。"))
        client.valve_client = FakeValve()
        self.assertEqual(asyncio.run(client.get_match_detail(1)), "valve-1")
        self.assertTrue(FakeValve.called)

    def test_heroes_failure_is_not_fatal_for_match_detail(self):
        """英雄表只是附加信息：拿不到时应退回 英雄#id，而不是让整场查询失败。"""
        from astrbot_plugin_dota2assistant_plus.core.models import MatchDetail

        self.assertTrue(hasattr(MatchDetail, "__dataclass_fields__"))
        # get_heroes(allow_failure=True) 在失败时应返回空表而不是抛出
        import asyncio

        client = self._client_with(raises=OpenDotaRequestError("英雄数据 请求超时。"))
        self.assertEqual(asyncio.run(client.get_heroes(allow_failure=True)), [])
        with self.assertRaises(OpenDotaRequestError):
            asyncio.run(client.get_heroes())

    def test_heroes_cache_avoids_repeat_requests(self):
        import asyncio

        calls = {"n": 0}

        client = OpenDotaClient(timeout=1)

        async def fake_get(path, params=None, context="OpenDota"):
            calls["n"] += 1
            return [
                {
                    "id": 1,
                    "name": "npc_dota_hero_antimage",
                    "localized_name": "Anti-Mage",
                }
            ]

        client._get = fake_get  # type: ignore[assignment]
        first = asyncio.run(client.get_heroes())
        second = asyncio.run(client.get_heroes())
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(calls["n"], 1, "英雄表应被缓存，不应重复请求")


class _FakeResponse:
    def __init__(self, status, payload=None, raise_exc=None):
        self.status = status
        self._payload = payload
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def json(self, content_type=None):
        if self._raise is not None:
            raise self._raise
        return self._payload


class _FakeSession:
    """按脚本依次返回响应；脚本用尽后重复最后一个。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def get(self, _url, params=None):
        self.calls += 1
        if len(self.script) > 1:
            return self.script.pop(0)
        return self.script[0]


class TestRequestRetryAndFailureClassification(unittest.TestCase):
    """`_get` 的重试与失败分类。

    只有把「请求失败」和「没有数据」分开，才能在网络抖动时重试或如实报错，
    而不是把超时说成「这场比赛/这个玩家不存在」。
    """

    def _patch(self, script):
        import astrbot_plugin_dota2assistant_plus.core.opendota as mod
        from unittest import mock

        session = _FakeSession(script)
        fake = mock.MagicMock()
        fake.ClientSession = lambda **_kw: session
        fake.ClientTimeout = mod.aiohttp.ClientTimeout
        fake.ClientError = mod.aiohttp.ClientError
        return mock.patch.object(mod, "aiohttp", fake), session

    def test_server_error_is_retried_then_succeeds(self):
        import asyncio

        patcher, session = self._patch([_FakeResponse(503), _FakeResponse(200, [1, 2])])
        with patcher:
            client = OpenDotaClient(timeout=1)
            result = asyncio.run(client._get("/x", context="T"))
        self.assertEqual(result, [1, 2])
        self.assertEqual(session.calls, 2, "503 后应重试一次")

    def test_server_error_exhausts_retries_and_raises(self):
        import asyncio

        patcher, session = self._patch([_FakeResponse(503)])
        with patcher:
            client = OpenDotaClient(timeout=1)
            with self.assertRaises(OpenDotaRequestError):
                asyncio.run(client._get("/x", context="T"))
        self.assertEqual(session.calls, 2, "应重试至上限")

    def test_timeout_is_retried_and_raises(self):
        import asyncio

        patcher, session = self._patch([_FakeResponse(200, raise_exc=TimeoutError())])
        with patcher:
            client = OpenDotaClient(timeout=1)
            with self.assertRaises(OpenDotaRequestError):
                asyncio.run(client._get("/x", context="T"))
        self.assertEqual(session.calls, 2, "超时应重试")

    def test_404_returns_none_without_retry(self):
        """404 表示资源确实不存在，属数据层面的『没有』，不重试。"""
        import asyncio

        patcher, session = self._patch([_FakeResponse(404)])
        with patcher:
            client = OpenDotaClient(timeout=1)
            self.assertIsNone(asyncio.run(client._get("/x", context="T")))
        self.assertEqual(session.calls, 1)

    def test_client_error_is_not_retried(self):
        """其余 4xx 是确定性错误，重试没有意义。"""
        import asyncio

        patcher, session = self._patch([_FakeResponse(403)])
        with patcher:
            client = OpenDotaClient(timeout=1)
            with self.assertRaises(OpenDotaRequestError):
                asyncio.run(client._get("/x", context="T"))
        self.assertEqual(session.calls, 1)

    def test_429_is_retried(self):
        import asyncio

        patcher, session = self._patch([_FakeResponse(429), _FakeResponse(200, {"ok": True})])
        with patcher:
            client = OpenDotaClient(timeout=1)
            self.assertEqual(asyncio.run(client._get("/x", context="T")), {"ok": True})
        self.assertEqual(session.calls, 2)


if __name__ == "__main__":
    unittest.main()
