import json
import threading
import unittest
import urllib.request
from urllib.parse import parse_qs, urlparse

import httpx

from agentcal.auth import (
    CallbackServer,
    MemoryTokenStore,
    OAuthClient,
    OAuthConfig,
    TokenProvider,
    TokenSet,
    authorize_url,
    login,
    pkce_pair,
)
from agentcal.calendar_client import AuthExpired

from tests.helpers import FakeClock

CFG = OAuthConfig(client_id="cid", client_secret="sec", redirect_uri="http://127.0.0.1:0/callback")


class AuthFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.now_epoch = self.clock().timestamp()

    def test_pkce_pair_shapes(self):
        verifier, challenge = pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertEqual(len(challenge), 43)
        import base64
        import hashlib

        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)

    def test_authorize_url_contains_required_params(self):
        url = authorize_url(CFG, "st", "ch")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["state"], ["st"])
        self.assertIn("calendar.events", query["scope"][0])

    def _client(self, handler):
        return OAuthClient(httpx.Client(transport=httpx.MockTransport(handler)), CFG, now=lambda: self.now_epoch)

    def test_exchange_parses_expiry(self):
        def handler(request):
            body = dict(parse_qs(request.content.decode()))
            self.assertEqual(body["code_verifier"], ["verifier"])
            return httpx.Response(
                200,
                json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "s"},
            )

        token = self._client(handler).exchange("code", "verifier")
        self.assertEqual(token.access_token, "at")
        self.assertAlmostEqual(token.expires_at, self.now_epoch + 3600)

    def test_refresh_keeps_existing_refresh_token(self):
        def handler(request):
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})

        fresh = self._client(handler).refresh(TokenSet("at1", "rt1", self.now_epoch - 10))
        self.assertEqual(fresh.access_token, "at2")
        self.assertEqual(fresh.refresh_token, "rt1")

    def test_token_endpoint_error_is_auth_expired(self):
        def handler(request):
            return httpx.Response(400, json={"error": "invalid_grant"})

        with self.assertRaises(AuthExpired):
            self._client(handler).exchange("bad", "v")

    def test_provider_refreshes_expired_token(self):
        calls = []

        def handler(request):
            calls.append(dict(parse_qs(request.content.decode())))
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

        store = MemoryTokenStore(TokenSet("old", "rt", self.now_epoch - 100))
        provider = TokenProvider(self._client(handler), store, self.clock)
        self.assertEqual(provider.access_token(), "fresh")
        self.assertEqual(calls[0]["grant_type"], ["refresh_token"])
        self.assertEqual(store.load().access_token, "fresh")

    def test_provider_without_token_raises(self):
        provider = TokenProvider(self._client(lambda request: httpx.Response(200, json={})), MemoryTokenStore(), self.clock)
        with self.assertRaises(AuthExpired):
            provider.access_token()

    def test_callback_server_returns_code_for_matching_state(self):
        with CallbackServer("http://127.0.0.1:0/callback") as server:
            def hit():
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.port}/callback?code=abc&state=st", timeout=5
                ).read()

            thread = threading.Thread(target=hit)
            thread.start()
            code = server.wait_for_code("st", timeout_s=5)
            thread.join(timeout=5)
            self.assertEqual(code, "abc")

    def test_callback_server_rejects_state_mismatch(self):
        with CallbackServer("http://127.0.0.1:0/callback") as server:
            def hit():
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.port}/callback?code=abc&state=wrong", timeout=5
                ).read()

            thread = threading.Thread(target=hit)
            thread.start()
            with self.assertRaises(AuthExpired):
                server.wait_for_code("expected", timeout_s=5)
            thread.join(timeout=5)

    def test_login_end_to_end_writes_store(self):
        store = MemoryTokenStore()

        def handler(request):
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

        def opener(url: str) -> None:
            query = parse_qs(urlparse(url).query)
            state = query["state"][0]
            redirect = urlparse(query["redirect_uri"][0])

            def hit():
                urllib.request.urlopen(
                    f"{redirect.scheme}://{redirect.netloc}{redirect.path}?code=good&state={state}",
                    timeout=5,
                ).read()

            threading.Thread(target=hit).start()

        token = login(
            httpx.Client(transport=httpx.MockTransport(handler)),
            CFG,
            store,
            clock=self.clock,
            timeout_s=10,
            out=lambda message: None,
            opener=opener,
        )
        self.assertEqual(token.access_token, "at")
        self.assertEqual(store.load().refresh_token, "rt")
