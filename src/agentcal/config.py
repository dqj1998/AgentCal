"""TOML configuration (design v4.0 §11)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoreConfig:
    calendar_id: str = "primary"
    owner_email: str = ""
    db_path: str = "agentcal.db"
    artifacts_dir: str = "artifacts"
    poll_interval_s: int = 60
    missed_policy: str = "run_late"  # run_late | ask
    grace_s: int = 600
    lease_ttl_s: int = 90
    heartbeat_s: int = 15
    max_attempts: int = 3
    calendar_enabled: bool = True
    default_agent: str = ""
    profile_wait_s: float = 30.0


@dataclass(frozen=True)
class PolicyConfig:
    allowed_users: tuple[str, ...] = ()
    allowed_chats: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    max_actions_per_min_per_domain: int = 6


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent allowlist. Absent section => fall back to policy.allowed_domains."""

    domains: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    sites: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class BrowserConfig:
    mode: str = "cdp"  # cdp | persistent
    cdp_endpoint: str = "http://127.0.0.1:9222"
    profile_dir: str = "profiles/main"


@dataclass(frozen=True)
class LlmConfig:
    proxy: str = "http://127.0.0.1:4000"
    t_auto: float = 0.90
    t_ask: float = 0.60
    daily_cost_alert_usd: float = 10.0
    model_slm: str = "qwen2.5:3b"
    model_vision: str = "qwen2.5vl:7b"
    price_in_per_1k: float = 0.0
    price_out_per_1k: float = 0.0


@dataclass(frozen=True)
class AuthConfig:
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8765/callback"


@dataclass(frozen=True)
class AppConfig:
    core: CoreConfig = field(default_factory=CoreConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    path: Path | None = None

    def to_public_dict(self) -> dict:
        return {
            "core": {"calendar_id": self.core.calendar_id, "missed_policy": self.core.missed_policy},
            "policy": {"allowed_domains": list(self.policy.allowed_domains)},
            "agents": {name: list(cfg.domains) for name, cfg in self.agents.items()},
            "browser": {"mode": self.browser.mode},
        }


def _core(raw: dict) -> CoreConfig:
    allowed = {
        "calendar_id", "owner_email", "db_path", "artifacts_dir", "poll_interval_s",
        "missed_policy", "grace_s", "lease_ttl_s", "heartbeat_s", "max_attempts",
        "calendar_enabled", "default_agent", "profile_wait_s",
    }
    return CoreConfig(**{k: v for k, v in raw.items() if k in allowed})


def _agents(raw: dict) -> dict[str, AgentConfig]:
    out: dict[str, AgentConfig] = {}
    for name, section in (raw or {}).items():
        out[name] = AgentConfig(
            domains=tuple(section.get("domains", ())),
            actions=tuple(section.get("actions", ())),
            sites={k: dict(v) for k, v in (section.get("sites") or {}).items()},
        )
    return out


def load(path: str | Path) -> AppConfig:
    p = Path(path)
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return AppConfig(
        core=_core(raw.get("core", {})),
        policy=PolicyConfig(**raw.get("policy", {})),
        browser=BrowserConfig(**raw.get("browser", {})),
        llm=LlmConfig(**raw.get("llm", {})),
        auth=AuthConfig(**raw.get("auth", {})),
        agents=_agents(raw.get("agents", {})),
        path=p,
    )
