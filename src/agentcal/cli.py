"""Command line entry point: local diagnostics and the long-running supervisor."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from . import secrets
from .agents import REGISTRY
from .auth import KeychainTokenStore, OAuthClient, OAuthConfig, TokenProvider, login
from .calendar_client import HttpCalendarTransport
from .clock import iso, utcnow
from .config import load
from .db import connect, migrate
from .executor import Executor
from .fsm import Fsm, new_id
from .http import make_client
from .llm.calibrate import calibrate
from .llm.router import LlmRouter
from .notifier import TelegramNotifier
from .poller import CalendarPoller
from .projector import CalendarProjector
from .runner.rpc import LocalRunner, WorkerRunner
from .scheduler import Scheduler
from .supervisor import Supervisor
from .telegram import TelegramChannel, TelegramClient
from .vault import Vault
from .voice import VoiceIntake, WhisperCpp


def _build_router(args, cfg, conn, clock):
    base = os.environ.get("AGENTCAL_LLM_BASE") or (cfg.llm.proxy if getattr(args, "llm", False) else None)
    if not base:
        return None
    return LlmRouter(make_client(), cfg, conn, clock, base_url=base)


def _runtime(args, *, offline: bool = False, local: bool = False):
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    clock = utcnow
    fsm = Fsm(conn, cfg, clock)
    vault = Vault(secrets.get_hex("vault"))
    router = _build_router(args, cfg, conn, clock)

    transport = None
    poller = None
    if not offline:
        transport = HttpCalendarTransport(make_client(), _token_provider(cfg, clock))
        if cfg.core.calendar_enabled:
            poller = CalendarPoller(
                conn, transport, cfg, secrets.get_hex("hmac"), fsm, clock, router=router
            )

    if local:
        runner = LocalRunner({name: cls({}) for name, cls in REGISTRY.items()})
    else:
        runner = WorkerRunner()

    scheduler = Scheduler(conn, cfg, fsm, clock, vault)
    executor = Executor(conn, cfg, fsm, clock, runner=runner, vault=vault)
    projector = CalendarProjector(conn, transport, cfg, clock)

    token = secrets.get_text("telegram_token")
    tg_client = TelegramClient(make_client(), token) if token else None
    channel = TelegramChannel(conn, tg_client, cfg, fsm, clock, router) if tg_client else None
    if channel is not None and os.environ.get("AGENTCAL_WHISPER_MODEL"):
        channel.voice = VoiceIntake(
            tg_client,
            WhisperCpp(model=os.environ["AGENTCAL_WHISPER_MODEL"]),
            Path(cfg.core.artifacts_dir) / "voice",
        )
    notifier = TelegramNotifier(conn, tg_client, cfg, clock)
    supervisor = Supervisor(
        conn, cfg, fsm, scheduler, executor, projector, poller, notifier, clock
    )
    return cfg, conn, fsm, supervisor, channel, tg_client


def _oauth_config(cfg) -> OAuthConfig:
    return OAuthConfig(
        client_id=cfg.auth.client_id,
        client_secret=cfg.auth.client_secret,
        redirect_uri=cfg.auth.redirect_uri,
    )


def _token_provider(cfg, clock):
    env_token = os.environ.get("AGENTCAL_ACCESS_TOKEN")
    if env_token:
        return lambda: env_token
    if not cfg.auth.client_id:
        raise SystemExit(
            "日历凭证缺失：运行 `agentcal auth login`（需在 [auth] 配 client_id），"
            "或设置 AGENTCAL_ACCESS_TOKEN 做临时联调。"
        )
    provider = TokenProvider(
        OAuthClient(make_client(), _oauth_config(cfg)),
        KeychainTokenStore(),
        clock,
    )
    return provider.access_token


def cmd_auth(args) -> int:
    cfg = load(args.config)
    clock = utcnow
    store = KeychainTokenStore()
    if args.auth_command == "status":
        if cfg.auth.client_id:
            provider = TokenProvider(
                OAuthClient(make_client(), _oauth_config(cfg)), store, clock
            )
            status = provider.status()
        else:
            token = store.load()
            status = {"token": token is not None, "client_id_configured": False}
        print(json.dumps(status, ensure_ascii=False))
        return 0

    if not cfg.auth.client_id:
        raise SystemExit("缺少 [auth] client_id：请先在 config 中配置 Google OAuth 客户端")
    token = login(
        make_client(),
        _oauth_config(cfg),
        store,
        clock=clock,
        timeout_s=args.timeout,
        opener=webbrowser.open if args.open else None,
    )
    print(
        "授权完成：expires_at="
        + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(token.expires_at))
        + " refresh_token="
        + ("yes" if token.refresh_token else "no")
    )
    return 0


def cmd_init_db(args) -> int:
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    applied = migrate(conn)
    print(f"db={cfg.core.db_path} migrations_applied={applied}")
    return 0


def cmd_add_task(args) -> int:
    cfg, conn, fsm, *_ = _runtime(args, offline=True, local=True)
    now_s = iso(utcnow())
    due = args.due or now_s
    vault = Vault(secrets.get_hex("vault"))
    task_id = new_id("tsk")
    conn.execute(
        """INSERT INTO tasks(id, idem_key, source, actor_id, title, intent_enc, agent, confidence,
             state, attempt, not_before, due_at, policy_snap, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,'pending',0,?,?,?,?,?)""",
        (
            task_id,
            f"cli:{task_id}",
            "cli",
            args.actor,
            args.text[:1024],
            vault.seal({"prompt": args.text, "agent": args.agent, "targets": args.target or []}),
            args.agent,
            None,
            due,
            due,
            json.dumps({"decision": "allow", "source": "cli"}),
            now_s,
            now_s,
        ),
    )
    print(task_id)
    return 0


def cmd_tick(args) -> int:
    cfg, conn, fsm, supervisor, channel, tg_client = _runtime(
        args, offline=args.offline, local=args.local
    )
    if channel is not None:
        channel.poll(timeout_s=args.poll_timeout)
    report = supervisor.tick_once()
    _maybe_cost_alert(conn, cfg, tg_client)
    print(json.dumps(report.__dict__, ensure_ascii=False, default=str))
    return 0


def cmd_serve(args) -> int:
    cfg, conn, fsm, supervisor, channel, tg_client = _runtime(
        args, offline=args.offline, local=args.local
    )
    logging.info("agentcal serving: poll=%ss telegram=%s", cfg.core.poll_interval_s, channel is not None)
    while True:
        started = time.monotonic()
        try:
            if channel is not None:
                # Long-poll paces the loop: a command wakes it and is executed in the same
                # iteration, instead of waiting for the next 60s calendar tick.
                channel.poll(timeout_s=args.poll_timeout)
            report = supervisor.tick_once()
            _maybe_cost_alert(conn, cfg, tg_client)
            logging.info("tick %s", report.__dict__)
        except Exception:  # noqa: BLE001 - the loop must survive one bad tick
            logging.exception("tick failed")
        if channel is None:
            time.sleep(max(cfg.core.poll_interval_s, 1))
        else:
            elapsed = time.monotonic() - started
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
    return 0


def _maybe_cost_alert(conn, cfg, client) -> None:
    if client is None:
        return
    day = int(utcnow().strftime("%Y%m%d"))
    row = conn.execute("SELECT offset FROM tg_state WHERE kind='cost_alert'").fetchone()
    if row is not None and int(row["offset"]) >= day:
        return
    today = iso(utcnow())[:10]
    total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_calls WHERE created_at LIKE ?",
        (today + "%",),
    ).fetchone()["c"]
    if float(total) < cfg.llm.daily_cost_alert_usd:
        return
    top = conn.execute(
        "SELECT r.task_id AS task_id, SUM(c.cost_usd) AS cost FROM llm_calls c "
        "JOIN runs r ON r.id=c.run_id GROUP BY r.task_id ORDER BY cost DESC LIMIT 3"
    ).fetchall()
    lines = ["当日模型成本 %.2f USD 已超过阈值 %.2f USD" % (float(total), cfg.llm.daily_cost_alert_usd)]
    for row in top:
        lines.append("%s: %.2f USD" % (row["task_id"], float(row["cost"])))
    chat = next(iter(cfg.policy.allowed_chats), None)
    if chat is not None:
        try:
            client.send_message(chat, chr(10).join(lines))
        except Exception:  # noqa: BLE001 - alerting must not crash the loop
            logging.warning("cost alert failed")
    conn.execute(
        "INSERT INTO tg_state(kind, offset) VALUES('cost_alert', ?) "
        "ON CONFLICT(kind) DO UPDATE SET offset=excluded.offset",
        (day,),
    )
    conn.commit()


def cmd_calibrate(args) -> int:
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    router = _build_router(args, cfg, conn, utcnow)
    if router is None:
        raise SystemExit("LLM 路由未配置：设置 AGENTCAL_LLM_BASE 或传 --llm")
    samples = []
    for line in Path(args.labels).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            samples.append(json.loads(line))
    report = calibrate(router, samples)
    print(
        json.dumps(
            {
                "total": report.total,
                "accuracy": round(report.accuracy, 4),
                "suggested_t_auto": report.suggested_t_auto,
                "suggested_t_ask": report.suggested_t_ask,
                "buckets": [
                    {
                        "range": "%.1f-%.1f" % (bucket.low, bucket.high),
                        "total": bucket.total,
                        "accuracy": round(bucket.accuracy, 3),
                    }
                    for bucket in report.buckets
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_domains(args) -> int:
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    from .policy import trusted_domains

    print(
        json.dumps(
            {
                "trusted": trusted_domains(conn),
                "agents": {name: list(spec.domains) for name, spec in cfg.agents.items()},
                "global": list(cfg.policy.allowed_domains),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_revoke_domain(args) -> int:
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    from .policy import revoke_domain

    ok = revoke_domain(conn, args.domain)
    print("revoked " + args.domain if ok else "not found")
    return 0 if ok else 1


def cmd_status(args) -> int:
    cfg = load(args.config)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    counts = {
        row["state"]: row["n"]
        for row in conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
    }
    print("states:", json.dumps(counts, ensure_ascii=False))
    for row in conn.execute(
        "SELECT id, state, attempt, agent, title FROM tasks ORDER BY created_at DESC LIMIT ?",
        (args.limit,),
    ):
        print(f"{row['id']}  {row['state']:<14} try={row['attempt']}  {row['agent'] or '-'}  {row['title'][:60]}")
    for row in conn.execute("SELECT task_id, reason FROM approvals WHERE decision IS NULL"):
        print(f"approval pending: {row['task_id']}  {row['reason']}")
    outbox = conn.execute("SELECT COUNT(*) AS n FROM projection_outbox").fetchone()["n"]
    notes = conn.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"]
    print(f"projection_outbox={outbox} notifications={notes}")
    return 0


def cmd_decide(args) -> int:
    cfg, conn, fsm, *_ = _runtime(args, offline=True, local=True)
    ok = fsm.resolve_approval(
        args.task_id, "approve" if args.command == "approve" else "deny", args.actor
    )
    print("ok" if ok else "no pending approval")
    return 0 if ok else 1


def cmd_retry(args) -> int:
    cfg, conn, fsm, *_ = _runtime(args, offline=True, local=True)
    ok = fsm.retry(args.task_id, args.actor)
    print("ok" if ok else "not retryable")
    return 0 if ok else 1


def cmd_project(args) -> int:
    cfg, conn, fsm, supervisor, *_ = _runtime(args, offline=args.offline, local=True)
    ok = supervisor.projector.project_task(args.task_id)
    print("projected" if ok else "deferred")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser("agentcal")
    parser.add_argument("--config", default="config/agentcal.toml")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create/migrate the local database")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("add-task", help="insert a task manually")
    p.add_argument("text")
    p.add_argument("--agent", default="noop")
    p.add_argument("--target", action="append", default=None)
    p.add_argument("--due", default=None)
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_add_task)

    p = sub.add_parser("tick", help="run one tick")
    p.add_argument("--once", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--local", action="store_true", help="run agents in-process")
    p.add_argument("--llm", action="store_true", help="enable the LLM router via llm.proxy")
    p.add_argument("--poll-timeout", type=int, default=0)
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("serve", help="long-running supervisor")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--local", action="store_true")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--poll-timeout", type=int, default=20)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("auth", help="Google OAuth login/status")
    p.add_argument("auth_command", choices=["login", "status"])
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--open", action="store_true", help="open the consent URL in the default browser")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("calibrate", help="score the router against a labelled JSONL set")
    p.add_argument("labels")
    p.add_argument("--llm", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("domains", help="list trusted and per-agent domains")
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser("revoke-domain", help="remove a permanently trusted domain")
    p.add_argument("domain")
    p.set_defaults(func=cmd_revoke_domain)

    p = sub.add_parser("status", help="show task states, approvals and queues")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="approve a blocked task")
    p.add_argument("task_id")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("deny", help="deny a blocked task")
    p.add_argument("task_id")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("retry", help="requeue a failed/canceled/blocked task")
    p.add_argument("task_id")
    p.add_argument("--actor", default="cli")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("project", help="force one calendar projection")
    p.add_argument("task_id")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_project)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return args.func(args)
    except secrets.SecretMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
