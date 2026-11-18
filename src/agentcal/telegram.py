"""Telegram control plane: allow-listed commands, callbacks, rate-limited replies."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import sqlite3
from datetime import timedelta

import httpx

from .clock import iso
from .config import AppConfig
from .db import tx
from .fsm import Fsm, new_id

log = logging.getLogger(__name__)

CALLBACK_MAX_BYTES = 64


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def short_id(task_id: str) -> str:
    """8 base32 chars (40 bits) - stable, collision-safe enough for one user's task list."""
    digest = hashlib.blake2b(task_id.encode(), digest_size=5).digest()
    return base64.b32encode(digest).decode().rstrip("=")[:8]


def callback_data(card: str, task_id: str, action: str) -> str:
    data = f"{card}:{short_id(task_id)}:{action}"
    if len(data.encode()) > CALLBACK_MAX_BYTES:
        raise ValueError("callback_data exceeds 64 bytes")
    return data


class ChatRateLimiter:
    """One message per chat per interval; callers skip instead of sleeping."""

    def __init__(self, clock, min_interval_s: float = 1.0):
        self.clock = clock
        self.min_interval_s = min_interval_s
        self._last: dict[str, float] = {}

    def ok(self, chat_id) -> bool:
        now = self.clock().timestamp()
        last = self._last.get(str(chat_id), 0.0)
        if now - last < self.min_interval_s:
            return False
        self._last[str(chat_id)] = now
        return True


class TelegramClient:
    def __init__(self, client: httpx.Client, token: str, base_url: str = "https://api.telegram.org"):
        self._client = client
        self._base = f"{base_url.rstrip('/')}/bot{token}"

    def _call(self, method: str, payload: dict, timeout: float = 30.0):
        try:
            response = self._client.post(f"{self._base}/{method}", json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code == 429:
            retry = float(response.headers.get("Retry-After", data.get("parameters", {}).get("retry_after", 1)))
            raise TelegramError(f"{method}: 429", 429, retry)
        if response.status_code >= 400 or not data.get("ok", True):
            raise TelegramError(f"{method}: {response.status_code} {data.get('description', '')}", response.status_code)
        return data.get("result")

    def get_updates(self, offset: int, timeout_s: int = 0, limit: int = 50) -> list[dict]:
        payload = {
            "offset": offset,
            "limit": limit,
            "timeout": timeout_s,
            "allowed_updates": ["message", "callback_query"],
        }
        return self._call("getUpdates", payload, timeout=timeout_s + 15) or []

    def send_message(self, chat_id, text: str, keyboard: dict | None = None, thread_id: int | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        if thread_id:
            payload["message_thread_id"] = thread_id
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id: int, text: str, keyboard: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        if keyboard:
            payload["reply_markup"] = keyboard
        return self._call("editMessageText", payload)

    def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", {"file_id": file_id})

    def download(self, file_path: str) -> bytes:
        url = f"{self._base.rsplit('/bot', 1)[0]}/file/bot{self._base.rsplit('/bot', 1)[1]}/{file_path}"
        response = self._client.get(url, timeout=60)
        if response.status_code >= 400:
            raise TelegramError(f"download: {response.status_code}", response.status_code)
        return response.content


class TelegramChannel:
    def __init__(self, conn: sqlite3.Connection, client: TelegramClient, cfg: AppConfig, fsm: Fsm, clock, router=None):
        self.conn = conn
        self.client = client
        self.cfg = cfg
        self.fsm = fsm
        self.clock = clock
        self.router = router
        self.voice = None  # set by CLI when whisper.cpp is configured
        self.offset = self._load_offset()

    # ------------------------------------------------------------------ public
    def poll(self, timeout_s: int = 0, limit: int = 50) -> int:
        try:
            updates = self.client.get_updates(self.offset, timeout_s=timeout_s, limit=limit)
        except TelegramError as exc:
            log.warning("telegram poll failed: %s", exc)
            return 0
        handled = 0
        for update in updates:
            try:
                self.handle_update(update)
                handled += 1
            except Exception:  # noqa: BLE001 - one bad update must not stop the loop
                log.exception("telegram update failed: %s", update.get("update_id"))
            self.offset = int(update.get("update_id", self.offset)) + 1
        if updates:
            self._save_offset()
        return handled

    def handle_update(self, update: dict) -> None:
        if "message" in update:
            self._on_message(update["message"])
        elif "callback_query" in update:
            self._on_callback(update["callback_query"])

    # ------------------------------------------------------------------ inbound
    def _on_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        if not self._authorized(user.get("id"), chat.get("id")):
            log.warning("telegram unauthorized actor=%s chat=%s", user.get("id"), chat.get("id"))
            return
        if message.get("voice") and self.voice is not None:
            from .voice import SttUnavailable

            try:
                transcript = self.voice.transcribe_message(message)
            except SttUnavailable as exc:
                self._reply(message, f"语音处理失败：{exc}")
                return
            if transcript:
                self._create_task(message, transcript)
            else:
                self._reply(message, "没听清，请重发或直接打字")
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        actor = str(user.get("id"))
        if command == "/start" or command == "/help":
            self._reply(message, "AgentCal\n/task <文本>\n/status\n/approve <id>\n/deny <id>\n/retry <id>\n/cancel <id>")
        elif command == "/status":
            self._reply(message, self._status_text())
        elif command == "/task":
            self._create_task(message, argument.strip())
        elif command in ("/approve", "/deny"):
            task_id = argument.strip()
            ok = self.fsm.resolve_approval(task_id, command[1:], actor)
            self._reply(message, "已处理" if ok else "没有待审批记录")
        elif command == "/retry":
            task_id = argument.strip()
            ok = self.fsm.retry(task_id, actor)
            self._reply(message, "已重排" if ok else "该任务当前不可重试")
        elif command == "/domains":
            self._reply(message, self._domains_text())
        elif command == "/revoke":
            host = argument.strip().lower()
            from .policy import revoke_domain

            with tx(self.conn):
                ok = revoke_domain(self.conn, host)
            self._reply(message, ("已撤销 " + host) if ok else "未找到该域名")
        elif command == "/cancel":
            ok = self.fsm.cancel(argument.strip(), actor, "telegram_cancel")
            self._reply(message, "已取消" if ok else "无法取消")
        else:
            self._reply(message, "未知命令，/help 查看")

    def _on_callback(self, callback: dict) -> None:
        user = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        if not self._authorized(user.get("id"), chat.get("id")):
            return
        try:
            self.client.answer_callback_query(callback.get("id", ""), "已受理")
        except TelegramError:
            pass
        parts = (callback.get("data") or "").split(":")
        if len(parts) != 3:
            return
        _card, short, action = parts
        row = self.conn.execute("SELECT task_id FROM tg_callbacks WHERE short_id=?", (short,)).fetchone()
        if row is None:
            self._send(chat.get("id"), "回调已过期")
            return
        task_id = row["task_id"]
        actor = str(user.get("id"))
        if action == "a":
            self.fsm.resolve_approval(task_id, "approve", actor)
            self._send(chat.get("id"), f"已批准 {task_id}")
        elif action == "d":
            self.fsm.resolve_approval(task_id, "deny", actor)
            self._send(chat.get("id"), f"已拒绝 {task_id}")
        elif action == "r":
            ok = self.fsm.retry(task_id, actor)
            self._send(chat.get("id"), "已重排" if ok else "不可重试")
        elif action == "t":
            self._send(chat.get("id"), self._status_text())

    # ------------------------------------------------------------------ outbound
    def _create_task(self, message: dict, text: str) -> None:
        if not text:
            self._reply(message, "用法：/task 描述你想做的事")
            return
        agent = None
        if self.router is not None:
            classification = self.router.classify(text, self.clock())
            if not classification.is_task:
                self._reply(message, "这看起来不像需要执行的任务")
                return
            agent = classification.agent
        agent = agent or (self.cfg.core.default_agent or None)
        if not agent:
            self._reply(message, "未配置 agent：设置 core.default_agent 或启用 LLM 路由")
            return
        chat = message.get("chat") or {}
        now_s = iso(self.clock())
        idem = f"tg:{chat.get('id')}:{message.get('message_id')}"
        parsed = {
            "title": text[:1024],
            "agent": agent,
            "prompt": text,
            "due_at": now_s,
            "calendar_id": None,
            "event_id": None,
        }
        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO commands(
                     id, idem_key, source, actor_id, raw_ref, parsed_json,
                     signature_ok, policy_verdict, received_at, applied_at)
                   VALUES(?,?,?,?,?,?,1,'allow',?,NULL)""",
                (
                    new_id("cmd"),
                    idem,
                    "telegram",
                    str((message.get("from") or {}).get("id", "")),
                    str(message.get("message_id", "")),
                    json.dumps(parsed, ensure_ascii=False),
                    now_s,
                ),
            )
        self._reply(message, "已受理" if cur.rowcount else "重复消息，已忽略")

    def _domains_text(self) -> str:
        from .policy import trusted_domains

        lines = ["\u5df2\u4fe1\u4efb\uff08\u6c38\u4e45\uff09\uff1a" + (", ".join(trusted_domains(self.conn)) or "\u65e0")]
        for name, spec in self.cfg.agents.items():
            lines.append("- " + name + ": " + (", ".join(spec.domains) or "\u65e0"))
        return chr(10).join(lines)

    def _status_text(self) -> str:
        rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
        counts = ", ".join(f"{row['state']}={row['n']}" for row in rows) or "无任务"
        pending = self.conn.execute(
            "SELECT task_id, reason FROM approvals WHERE decision IS NULL LIMIT 5"
        ).fetchall()
        lines = [counts]
        for row in pending:
            lines.append(f"待审批 {row['task_id']} ({row['reason']})")
        return "\n".join(lines)

    def _reply(self, message: dict, text: str) -> None:
        chat = message.get("chat") or {}
        self._send(chat.get("id"), text, message.get("message_thread_id"))

    def _send(self, chat_id, text: str, thread_id=None) -> None:
        try:
            self.client.send_message(chat_id, text, thread_id=thread_id)
        except TelegramError as exc:
            log.warning("telegram send failed: %s", exc)

    # ------------------------------------------------------------------ helpers
    def _authorized(self, user_id, chat_id) -> bool:
        users = {str(u) for u in self.cfg.policy.allowed_users}
        chats = {str(c) for c in self.cfg.policy.allowed_chats}
        if not users or not chats:
            return False
        return str(user_id) in users and str(chat_id) in chats

    def _load_offset(self) -> int:
        row = self.conn.execute("SELECT offset FROM tg_state WHERE kind='updates'").fetchone()
        return row["offset"] if row else 0

    def _save_offset(self) -> None:
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO tg_state(kind, offset) VALUES('updates', ?) "
                "ON CONFLICT(kind) DO UPDATE SET offset=excluded.offset",
                (self.offset,),
            )
