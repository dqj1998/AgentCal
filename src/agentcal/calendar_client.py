"""Google Calendar REST transport. Errors are mapped to domain exceptions."""
from __future__ import annotations

from typing import Callable, Protocol

import httpx

BASE_URL = "https://www.googleapis.com/calendar/v3"


class CalendarError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class SyncTokenInvalid(CalendarError):
    """HTTP 410: cursor must be rebuilt with a full sync."""


class PreconditionFailed(CalendarError):
    """HTTP 412: someone wrote the event between get and patch."""


class RateLimited(CalendarError):
    def __init__(self, retry_after: float = 5.0):
        super().__init__("rate limited", 429)
        self.retry_after = retry_after


class AuthExpired(CalendarError):
    """HTTP 401/403 with invalid credentials."""


class CalendarTransport(Protocol):
    def list_events(self, calendar_id: str, **kwargs) -> dict: ...
    def get_event(self, calendar_id: str, event_id: str) -> dict: ...
    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str) -> dict: ...


class HttpCalendarTransport:
    def __init__(
        self,
        client: httpx.Client,
        token_provider: Callable[[], str],
        base_url: str = BASE_URL,
    ):
        self._client = client
        self._token = token_provider
        self._base = base_url.rstrip("/")

    def _headers(self, etag: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        if etag:
            headers["If-Match"] = etag
        return headers

    def _check(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 410:
            raise SyncTokenInvalid("sync token invalid", 410)
        if response.status_code == 412:
            raise PreconditionFailed("etag mismatch", 412)
        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "5") or 5)
            raise RateLimited(retry_after)
        if response.status_code in (401, 403):
            raise AuthExpired(f"auth rejected: {response.text[:200]}", response.status_code)
        raise CalendarError(f"calendar error {response.status_code}: {response.text[:200]}", response.status_code)

    def list_events(
        self,
        calendar_id: str,
        *,
        sync_token: str | None = None,
        page_token: str | None = None,
        time_min: str | None = None,
        single_events: bool = True,
        show_deleted: bool = True,
        max_results: int = 250,
    ) -> dict:
        params = {
            "singleEvents": str(single_events).lower(),
            "showDeleted": str(show_deleted).lower(),
            "maxResults": max_results,
        }
        if sync_token:
            params["syncToken"] = sync_token
        if time_min:
            params["timeMin"] = time_min
        if page_token:
            params["pageToken"] = page_token
        response = self._client.get(
            f"{self._base}/calendars/{calendar_id}/events", params=params, headers=self._headers()
        )
        self._check(response)
        return response.json()

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        response = self._client.get(
            f"{self._base}/calendars/{calendar_id}/events/{event_id}", headers=self._headers()
        )
        self._check(response)
        data = response.json()
        data.setdefault("etag", response.headers.get("ETag", ""))
        return data

    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str) -> dict:
        response = self._client.patch(
            f"{self._base}/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": "none"},
            json=body,
            headers=self._headers(etag),
        )
        self._check(response)
        data = response.json()
        data.setdefault("etag", response.headers.get("ETag", ""))
        return data
