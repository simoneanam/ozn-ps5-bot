import os
import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from curl_cffi import AsyncSession

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "unused-test-token")
import vinted_watch as watcher


def response(status, data):
    return httpx.Response(status, json=data, request=httpx.Request(
        "GET", "https://www.vinted.it/web/gateway/svc-catalogue/items"
    ))


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncSession()
        self.client.head = AsyncMock(return_value=response(200, {}))
        self.items = [{"id": 123, "title": "PS5", "price": {
            "amount": "300.00", "currency_code": "EUR"
        }, "url": "/items/123-ps5", "photo": {"url": "https://example.com/ps5.jpg"}}]
        self.client.get = AsyncMock(return_value=response(200, {"items": self.items}))

    async def asyncTearDown(self):
        await self.client.close()

    async def test_current_endpoint_preserves_filters_and_items(self):
        self.assertEqual(await watcher.search_vinted(self.client), self.items)
        args, kwargs = self.client.get.call_args
        self.assertEqual(args[0], watcher.VINTED_BASE_URL + "/web/gateway/svc-catalogue/items")
        params = kwargs["params"]
        self.assertEqual(params["search_text"], watcher.SEARCH_TEXT)
        self.assertEqual(params["price_from"], watcher.PRICE_FROM)
        self.assertEqual(params["price_to"], watcher.PRICE_TO)
        self.assertEqual(params["currency"], "EUR")
        self.assertEqual(params["order"], "newest_first")
        self.assertEqual(params["per_page"], watcher.PER_PAGE)
        self.assertEqual(params["page"], 1)
        self.assertEqual(watcher.get_item_price(self.items[0]), "300.00 €")
        self.assertEqual(watcher.get_item_url(self.items[0]), "https://www.vinted.it/items/123-ps5")

    async def test_empty_catalog(self):
        self.client.get.return_value = response(200, {"items": []})
        self.assertEqual(await watcher.search_vinted(self.client), [])

    async def test_malformed_payload_is_not_an_empty_catalog(self):
        for payload in ({}, {"items": None}, {"items": {}}, {"items": [None]}):
            with self.subTest(payload=payload):
                self.client.get.return_value = response(200, payload)
                with self.assertRaises(RuntimeError):
                    await watcher.search_vinted(self.client)

    async def test_auth_refresh_is_retried_once(self):
        self.client.cookies.set("session", "test")
        self.client.get.side_effect = [response(401, {}), response(200, {"items": self.items})]
        self.assertEqual(await watcher.search_vinted(self.client), self.items)
        self.assertEqual(self.client.get.await_count, 2)
        self.client.head.assert_awaited_once()

    async def test_http_errors_are_not_swallowed(self):
        for status, expected_calls in ((404, 1), (429, 1), (403, 2)):
            with self.subTest(status=status):
                self.client.get.reset_mock()
                self.client.get.return_value = response(status, {})
                with self.assertRaises(httpx.HTTPStatusError):
                    await watcher.search_vinted(self.client)
                self.assertEqual(self.client.get.await_count, expected_calls)


class FilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.item = {"id": 1, "title": "PS5 con scatola originale",
                     "price": {"amount": "350", "currency_code": "EUR"},
                     "photo": {"url": "https://example.com/photo.jpg"}}
        for name, value in (("PRICE_FROM", 300), ("PRICE_TO", 450),
                            ("REQUIRE_PHOTO", True)):
            mock = patch.object(watcher, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_complete_listing_passes(self):
        self.assertIsNone(watcher.rejection_reason(self.item))

    def test_suspicious_text_is_filtered(self):
        for text in ("PS5 SOLO SCATOLA", "PS5 non-funzionante", "Scrivimi su WhatsApp"):
            with self.subTest(text=text):
                self.assertIsNotNone(watcher.rejection_reason(
                    dict(self.item, description=text)))

    def test_missing_photo_and_invalid_prices_are_filtered(self):
        self.assertIsNotNone(watcher.rejection_reason(dict(self.item, photo=None)))
        for amount in (None, "NaN", "Infinity", "abc", "0", "299", "451"):
            with self.subTest(amount=amount):
                self.assertIsNotNone(watcher.rejection_reason(dict(
                    self.item, price={"amount": amount, "currency_code": "EUR"})))
        for currency in (None, "USD"):
            self.assertIsNotNone(watcher.rejection_reason(dict(
                self.item, price={"amount": "350", "currency_code": currency})))

class RedisBackend:
    """In-memory REST double; each SeenStore uses the actual HTTP adapter."""
    def __init__(self):
        self.values = {}
        self.scores = {}
        self.fail_command = None
        self.commands = []

    def handle(self, request):
        args = json.loads(request.content)
        self.commands.append(args)
        command, key, *rest = args
        if command == self.fail_command:
            return httpx.Response(500, json={"error": "secret-must-not-leak"})
        if command == "GET":
            result = self.values.get(key)
        elif command == "SET":
            self.values[key] = rest[0]
            result = "OK"
        elif command == "ZADD":
            scores = self.scores.setdefault(key, {})
            result = 0
            for score, member in zip(rest[::2], rest[1::2]):
                result += member not in scores
                scores[member] = score
        elif command == "ZMSCORE":
            result = [self.scores.get(key, {}).get(member) for member in rest]
        elif command == "ZREMRANGEBYSCORE":
            scores = self.scores.setdefault(key, {})
            expired = [member for member, score in scores.items() if score < int(rest[1][1:])]
            for member in expired:
                del scores[member]
            result = len(expired)
        else:
            raise AssertionError(args)
        return httpx.Response(200, json={"result": result})


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = RedisBackend()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.backend.handle))
        self.addAsyncCleanup(self.client.aclose)
        self.store = self.new_store()
        self.item = {"id": 1, "title": "PS5", "price": {
            "amount": "350", "currency_code": "EUR"},
            "photo": {"url": "https://example.com/photo.jpg"}}
        mock = patch.object(watcher, "send_item", new_callable=AsyncMock)
        self.send = mock.start()
        self.addCleanup(mock.stop)
        for name, value in (("PRICE_FROM", 300), ("PRICE_TO", 450)):
            mock = patch.object(watcher, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def new_store(self):
        return watcher.SeenStore(self.client, "https://redis.example", "test", "test", 30)

    async def scan(self, items, store=None):
        await watcher.process_items(self.client, "chat", store or self.store, items)

    async def test_baseline_and_separate_run_deduplication(self):
        await self.scan([self.item])
        self.send.assert_not_awaited()
        second = dict(self.item, id=2)
        await self.scan([second, second, self.item], self.new_store())
        self.send.assert_awaited_once_with(self.client, "chat", second)
        await self.scan([second, self.item], self.new_store())
        self.assertEqual(self.send.await_count, 1)

    async def test_empty_baseline_does_not_silence_next_run(self):
        await self.scan([])
        await self.scan([self.item])
        self.send.assert_awaited_once()

    async def test_baseline_records_filtered_items_too(self):
        await self.scan([dict(self.item, title="solo scatola")])
        await self.scan([self.item])
        self.send.assert_not_awaited()

    async def test_telegram_failure_retries_only_unsent(self):
        await self.scan([])
        second = dict(self.item, id=2)
        self.send.side_effect = [None, RuntimeError("Telegram failure")]
        with self.assertRaises(RuntimeError):
            await self.scan([second, self.item])
        self.assertEqual(await self.store.known_ids(["1", "2"]), {"1"})
        self.send.side_effect = None
        self.send.reset_mock()
        await self.scan([second, self.item], self.new_store())
        self.send.assert_awaited_once_with(self.client, "chat", second)

    async def test_failed_initialization_does_not_enable_notifications(self):
        self.backend.fail_command = "SET"
        with self.assertRaises(httpx.HTTPStatusError):
            await self.scan([self.item])
        self.assertFalse(await self.store.initialized())
        self.backend.fail_command = None
        await self.scan([self.item, dict(self.item, id=2)])
        self.send.assert_not_awaited()

    async def test_redis_read_failure_prevents_sends(self):
        await self.scan([])
        self.backend.fail_command = "ZMSCORE"
        with self.assertRaises(httpx.HTTPStatusError):
            await self.scan([self.item])
        self.send.assert_not_awaited()

    async def test_redis_write_failure_stops_after_first_delivery(self):
        await self.scan([])
        self.backend.fail_command = "ZADD"
        with self.assertRaises(httpx.HTTPStatusError):
            await self.scan([dict(self.item, id=2), self.item])
        self.send.assert_awaited_once()
        self.assertEqual(await self.store.known_ids(["1"]), set())

    async def test_refresh_visible_and_prune_absent_old_ids(self):
        with patch.object(watcher.time, "time", return_value=1):
            await self.scan([self.item, dict(self.item, id=2)])
        with patch.object(watcher.time, "time", return_value=40 * 86400):
            await self.scan([self.item])
        self.assertEqual(await self.store.known_ids(["1", "2"]), {"1"})
        self.assertTrue(await self.store.initialized())
        self.send.assert_not_awaited()

    async def test_rejected_new_listing_is_rechecked(self):
        await self.scan([])
        await self.scan([dict(self.item, title="solo scatola")])
        self.send.assert_not_awaited()
        await self.scan([self.item])
        self.send.assert_awaited_once()

    async def test_redis_error_payload_and_timeout_fail_closed(self):
        def timeout(request):
            raise httpx.ReadTimeout("credential-must-not-leak", request=request)

        for handler in (
            lambda request: httpx.Response(200, json={"error": "WRONGTYPE"}),
            lambda request: httpx.Response(200, json={"unexpected": None}),
            timeout,
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                store = watcher.SeenStore(client, "https://redis.example", "test", "test", 30)
                with self.assertRaises((RuntimeError, httpx.ReadTimeout)):
                    await self.scan([self.item], store)
        self.send.assert_not_awaited()


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "test-chat",
            "UPSTASH_REDIS_REST_URL": "https://redis.example",
            "UPSTASH_REDIS_REST_TOKEN": "test-redis-token",
        })
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("TELEGRAM_BOT_TOKEN", "test-token"), ("TELEGRAM_CHAT_ID", "test-chat")):
            mock = patch.object(watcher, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    async def test_main_scans_once_and_returns(self):
        with patch.object(watcher, "search_vinted", new_callable=AsyncMock, return_value=[]) as search, \
             patch.object(watcher, "process_items", new_callable=AsyncMock) as process:
            self.assertEqual(await watcher.main(), 0)
        search.assert_awaited_once()
        process.assert_awaited_once()

    async def test_vinted_failure_alerts_without_storage_or_secret_leak(self):
        output = io.StringIO()
        with patch.object(watcher, "search_vinted", side_effect=httpx.ReadTimeout("secret-token")), \
             patch.object(watcher, "process_items", new_callable=AsyncMock) as process, \
             patch.object(watcher, "telegram_call", new_callable=AsyncMock) as telegram, \
             contextlib.redirect_stdout(output):
            self.assertEqual(await watcher.main(), 1)
        process.assert_not_awaited()
        telegram.assert_awaited_once()
        alert = telegram.call_args.kwargs["json"]["text"]
        self.assertIn("Vinted", alert)
        self.assertIn("ReadTimeout", alert)
        self.assertNotIn("secret-token", alert + output.getvalue())

    async def test_alert_failure_is_not_recursive_and_job_still_fails(self):
        with patch.object(watcher, "search_vinted", side_effect=RuntimeError("failure")), \
             patch.object(watcher, "telegram_call", side_effect=httpx.ReadTimeout("secret")) as call:
            self.assertEqual(await watcher.main(), 1)
        call.assert_awaited_once()

    async def test_redis_or_telegram_failure_alerts(self):
        with patch.object(watcher, "search_vinted", new_callable=AsyncMock, return_value=[]), \
             patch.object(watcher, "process_items", side_effect=RuntimeError("secret")), \
             patch.object(watcher, "notify_error", new_callable=AsyncMock) as alert:
            self.assertEqual(await watcher.main(), 1)
        self.assertIn("Redis/Telegram", alert.call_args.args[0])
        self.assertNotIn("secret", alert.call_args.args[0])

    async def test_missing_redis_config_alerts_before_scanning(self):
        with patch.dict(os.environ, {"UPSTASH_REDIS_REST_TOKEN": ""}), \
             patch.object(watcher, "search_vinted", new_callable=AsyncMock) as search, \
             patch.object(watcher, "notify_error", new_callable=AsyncMock) as alert:
            self.assertEqual(await watcher.main(), 1)
        search.assert_not_awaited()
        alert.assert_awaited_once()

    async def test_invalid_numeric_configuration_sends_alert(self):
        with patch.dict(os.environ, {"VINTED_PRICE_FROM": "invalid"}), \
             patch.object(watcher, "notify_error", new_callable=AsyncMock) as alert:
            self.assertEqual(await watcher.main(), 1)
        self.assertIn("configurazione", alert.call_args.args[0])

    async def test_ambiguous_photo_timeout_never_falls_back_to_text(self):
        with patch.object(watcher, "telegram_call", side_effect=httpx.ReadTimeout("secret")) as call:
            with self.assertRaises(httpx.ReadTimeout):
                await watcher.send_item(None, "chat", {"id": 1, "photo": {"url": "https://example.com/a.jpg"}})
        call.assert_awaited_once()

    async def test_rejected_photo_can_fall_back_to_text(self):
        rejected = response(400, {})
        error = httpx.HTTPStatusError("rejected", request=rejected.request, response=rejected)
        with patch.object(watcher, "telegram_call", side_effect=[error, {}]) as call:
            await watcher.send_item(None, "chat", {"id": 1, "photo": {"url": "https://example.com/a.jpg"}})
        self.assertEqual([args.args[1] for args in call.call_args_list], ["sendPhoto", "sendMessage"])


if __name__ == "__main__":
    unittest.main()
