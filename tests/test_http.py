import unittest

import httpx

from agentcal.calendar_client import (
    AuthExpired,
    HttpCalendarTransport,
    PreconditionFailed,
    RateLimited,
    SyncTokenInvalid,
)


def make_transport(handler) -> HttpCalendarTransport:
    return HttpCalendarTransport(httpx.Client(transport=httpx.MockTransport(handler)), lambda: "tok")


class HttpCalendarTest(unittest.TestCase):
    def test_410_maps_to_sync_token_invalid(self):
        transport = make_transport(lambda request: httpx.Response(410, json={}))
        with self.assertRaises(SyncTokenInvalid):
            transport.list_events("cal", sync_token="stale")

    def test_429_carries_retry_after(self):
        transport = make_transport(
            lambda request: httpx.Response(429, headers={"Retry-After": "7"}, json={})
        )
        with self.assertRaises(RateLimited) as ctx:
            transport.list_events("cal")
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_412_maps_to_precondition_failed(self):
        transport = make_transport(lambda request: httpx.Response(412, json={}))
        with self.assertRaises(PreconditionFailed):
            transport.patch_event("cal", "evt", {}, "etag")

    def test_401_maps_to_auth_expired(self):
        transport = make_transport(lambda request: httpx.Response(401, json={}))
        with self.assertRaises(AuthExpired):
            transport.get_event("cal", "evt")

    def test_patch_sends_if_match_and_send_updates(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["if_match"] = request.headers.get("If-Match")
            captured["send_updates"] = request.url.params.get("sendUpdates")
            return httpx.Response(200, json={"updated": "u2", "etag": "e2"})

        transport = make_transport(handler)
        result = transport.patch_event("cal", "evt", {"colorId": "5"}, "e1")
        self.assertEqual(captured["if_match"], "e1")
        self.assertEqual(captured["send_updates"], "none")
        self.assertEqual(result["updated"], "u2")

    def test_get_event_falls_back_to_etag_header(self):
        transport = make_transport(
            lambda request: httpx.Response(200, json={"id": "evt"}, headers={"ETag": "e9"})
        )
        self.assertEqual(transport.get_event("cal", "evt")["etag"], "e9")
