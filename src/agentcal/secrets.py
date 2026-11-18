"""Secret lookup: env var first (dev/CI), macOS Keychain second (runtime)."""
from __future__ import annotations

import os
import subprocess

KEYCHAIN_SERVICE = "agentcal"


class SecretMissing(RuntimeError):
    pass


def get_hex(name: str) -> bytes:
    """name: 'hmac' | 'vault'. Hex-encoded 32-byte key."""
    env = os.environ.get(f"AGENTCAL_{name.upper()}_HEX")
    if env:
        return bytes.fromhex(env.strip())
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return bytes.fromhex(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
        raise SecretMissing(
            f"missing {name} key: set AGENTCAL_{name.upper()}_HEX or store it in Keychain "
            f"(service={KEYCHAIN_SERVICE}, account={name})"
        ) from exc


def get_text(name: str) -> str | None:
    """Optional plain-text secret (e.g. telegram_token) from env or Keychain."""
    env = os.environ.get(f"AGENTCAL_{name.upper()}")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        value = out.stdout.strip()
        return value or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def keychain_hint(name: str) -> str:
    return f"security add-generic-password -s {KEYCHAIN_SERVICE} -a {name} -w <hex-32-bytes>"
