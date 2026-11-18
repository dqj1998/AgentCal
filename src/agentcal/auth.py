"""Google OAuth installed-app flow: PKCE, loopback callback, keychain store, refresh."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .calendar_client import AuthExpired

KEYCHAIN_SERVICE = "agentcal"
KEYCHAIN_ACCOUNT = "oauth_token"

DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.settings.readonly",
)


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str = ""
    auth_uri: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    scopes: tuple[str, ...] = DEFAULT_SCOPES


def authorize_url(cfg: OAuthConfig, state: str, challenge: str) -> str:
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(cfg.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return cfg.auth_uri + "?" + urlencode(params)


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    scope: str = ""
    token_type: str = "Bearer"

    def expired(self, now_epoch: float, leeway: float = 60.0) -> bool:
        return self.expires_at - leeway <= now_epoch

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
                "token_type": self.token_type,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "TokenSet":
        data = json.loads(raw)
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )


class TokenStore(Protocol):
    def load(self) -> TokenSet | None: ...
    def save(self, token: TokenSet) -> None: ...
    def clear(self) -> None: ...


class MemoryTokenStore:
    def __init__(self, token: TokenSet | None = None):
        self.token = token

    def load(self) -> TokenSet | None:
        return self.token

    def save(self, token: TokenSet) -> None:
        self.token = token

    def clear(self) -> None:
        self.token = None


class KeychainTokenStore:
    """Stores the token JSON as a generic password; never writes it to disk."""

    def __init__(self, service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT):
        self.service = service
        self.account = account

    def load(self) -> TokenSet | None:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", self.service, "-a", self.account, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        try:
            return TokenSet.from_json(result.stdout.strip())
        except (ValueError, KeyError):
            return None

    def save(self, token: TokenSet) -> None:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                self.service,
                "-a",
                self.account,
                "-w",
                token.to_json(),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )

    def clear(self) -> None:
        subprocess.run(
            ["security", "delete-generic-password", "-s", self.service, "-a", self.account],
            check=False,
            capture_output=True,
            timeout=5,
        )


class OAuthClient:
    def __init__(self, http: httpx.Client, cfg: OAuthConfig, now: Callable[[], float] = time.time):
        self.http = http
        self.cfg = cfg
        self.now = now

    def exchange(self, code: str, verifier: str) -> TokenSet:
        payload = {
            "client_id": self.cfg.client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": self.cfg.redirect_uri,
        }
        if self.cfg.client_secret:
            payload["client_secret"] = self.cfg.client_secret
        return self._post(payload)

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise AuthExpired("refresh_token_missing")
        payload = {
            "client_id": self.cfg.client_id,
            "refresh_token": token.refresh_token,
            "grant_type": "refresh_token",
        }
        if self.cfg.client_secret:
            payload["client_secret"] = self.cfg.client_secret
        fresh = self._post(payload)
        if fresh.refresh_token is None:
            fresh.refresh_token = token.refresh_token
        return fresh

    def _post(self, payload: dict) -> TokenSet:
        try:
            response = self.http.post(self.cfg.token_uri, data=payload, timeout=30)
        except httpx.HTTPError as exc:
            raise AuthExpired(f"token_endpoint_unreachable:{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise AuthExpired(f"token_endpoint_{response.status_code}:{response.text[:120]}")
        data = response.json()
        if "access_token" not in data:
            raise AuthExpired("token_response_missing_access_token")
        expires_in = float(data.get("expires_in", 3600))
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=self.now() + expires_in,
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )


class TokenProvider:
    """Returns a valid access token, refreshing transparently. Raises AuthExpired when human action is needed."""

    def __init__(self, client: OAuthClient, store: TokenStore, clock):
        self.client = client
        self.store = store
        self.clock = clock

    def access_token(self) -> str:
        token = self.store.load()
        if token is None:
            raise AuthExpired("no_token")
        if token.expired(self.clock().timestamp()):
            token = self.client.refresh(token)
            self.store.save(token)
        return token.access_token

    def status(self) -> dict:
        token = self.store.load()
        if token is None:
            return {"token": False}
        return {
            "token": True,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(token.expires_at)),
            "expired": token.expired(self.clock().timestamp()),
            "refresh_token": bool(token.refresh_token),
            "scope": token.scope,
        }


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_error(404)
            return
        params = parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]
        if error:
            self.server.result = ("error", error)
        elif state != self.server.expected_state:
            self.server.result = ("error", "state_mismatch")
        elif not code:
            self.server.result = ("error", "missing_code")
        else:
            self.server.result = ("code", code)
        body = "<html><body>AgentCal 授权完成，可以关闭此页面。</body></html>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()

    def log_message(self, *args):
        return


class CallbackServer:
    def __init__(self, redirect_uri: str):
        parsed = urlparse(redirect_uri)
        self.callback_path = parsed.path or "/callback"
        port = parsed.port if parsed.port is not None else 8765
        self._server = HTTPServer((parsed.hostname or "127.0.0.1", port), _CallbackHandler)
        self._server.callback_path = self.callback_path
        self._server.expected_state = ""
        self._server.result = ("error", "timeout")
        self._server.done = threading.Event()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.callback_path}"

    def __enter__(self) -> "CallbackServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait_for_code(self, expected_state: str, timeout_s: float = 300.0) -> str:
        self._server.expected_state = expected_state
        if not self._server.done.wait(timeout_s):
            raise AuthExpired("authorization_timeout")
        kind, value = self._server.result
        if kind != "code":
            raise AuthExpired(value)
        return value


def login(
    http: httpx.Client,
    cfg: OAuthConfig,
    store: TokenStore,
    *,
    clock,
    timeout_s: float = 300.0,
    out: Callable[[str], None] = print,
    opener: Callable[[str], None] | None = None,
) -> TokenSet:
    """Full loopback flow. The redirect URI's port is taken from cfg unless it is 0."""
    with CallbackServer(cfg.redirect_uri) as server:
        effective = OAuthConfig(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            auth_uri=cfg.auth_uri,
            token_uri=cfg.token_uri,
            redirect_uri=server.redirect_uri,
            scopes=cfg.scopes,
        )
        verifier, challenge = pkce_pair()
        state = pysecrets.token_urlsafe(24)
        url = authorize_url(effective, state, challenge)
        out("在浏览器中打开以下地址完成授权：")
        out(url)
        if opener is not None:
            opener(url)
        code = server.wait_for_code(state, timeout_s)
    token = OAuthClient(http, effective, now=lambda: clock().timestamp()).exchange(code, verifier)
    store.save(token)
    return token
