"""HTTP client factory: survives broken SSL_CERT_FILE placeholders (CI/sandbox)."""
from __future__ import annotations

import os
from pathlib import Path

import httpx


def make_client(timeout: float = 30.0, **kwargs) -> httpx.Client:
    cert = os.environ.get("SSL_CERT_FILE")
    if cert:
        path = Path(cert)
        if not path.is_file() or path.stat().st_size == 0:
            # Placeholder trust bundle (empty file): fall back to the system store
            # instead of crashing when httpx builds its SSL context.
            kwargs.setdefault("trust_env", False)
    return httpx.Client(timeout=timeout, **kwargs)
