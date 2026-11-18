"""Agent registry. Definitions are code + config, never files dropped into the runtime."""
from __future__ import annotations

from ..runner.protocol import AgentError
from .blogger import BloggerAgent
from .hashnode import HashnodeAgent
from .noop import NullAgent
from .security_scan import SecurityScanAgent
from .telegraph import TelegraphAgent
from .x_publisher import XPublisherAgent

REGISTRY = {
    "noop": NullAgent,
    "security-scan": SecurityScanAgent,
    "telegraph": TelegraphAgent,
    "blogger": BloggerAgent,
    "hashnode": HashnodeAgent,
    "x-publisher": XPublisherAgent,
}

# Agents that need the single Chromium profile lock. Everything else runs lock-free.
NEEDS_PROFILE = {"x-publisher", "blogger"}


def build_agent(name: str, settings: dict):
    cls = REGISTRY.get(name)
    if cls is None:
        raise AgentError("policy", f"unknown_agent:{name}")
    return cls(settings)
