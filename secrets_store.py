"""Credential lookup: environment first, macOS Keychain as a local fallback.

Deployment sets real environment variables (Render's dashboard, or `gh secret`
for CI). Locally the convention for this project is that secrets live in the
macOS Keychain rather than a `.env` file, so a plaintext credential never sits
in the working tree waiting to be committed by accident.

Nothing here ever logs a secret's value -- only whether one was found.

Store a credential with:

    security add-generic-password -a <account> -s FRED_API_KEY -w '<value>' -U
"""

import logging
import os
import platform
import shutil
import subprocess
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Resolved lookups, including misses. Shelling out to `security` on every
# request would add process-spawn latency to the hot path for no benefit --
# a credential does not change while the process is running.
_resolved: Dict[str, Optional[str]] = {}

KEYCHAIN_TIMEOUT_SECONDS = 5


def _from_keychain(name: str, account: Optional[str]) -> Optional[str]:
    """Read a generic password from the macOS Keychain, if available.

    Returns None on any failure. A missing credential is a normal state: the
    caller reports the feature unavailable rather than guessing a value.
    """
    if platform.system() != "Darwin":
        return None
    security = shutil.which("security")
    if not security:
        return None

    command = [security, "find-generic-password", "-s", name, "-w"]
    if account:
        command[2:2] = ["-a", account]

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug(f"Keychain lookup for {name} failed: {type(e).__name__}")
        return None

    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def get(name: str, account: Optional[str] = None) -> Optional[str]:
    """Return a credential, or None if it is not configured anywhere.

    Environment wins so that deployment and CI stay authoritative and a stale
    Keychain entry on a developer's laptop cannot silently override them.
    """
    from_env = os.environ.get(name)
    if from_env:
        return from_env

    if name in _resolved:
        return _resolved[name]

    value = _from_keychain(name, account)
    _resolved[name] = value
    logger.info(f"Credential {name}: {'found in keychain' if value else 'not configured'}")
    return value


def status(names) -> Dict[str, bool]:
    """Report which credentials are configured, without revealing any value."""
    return {name: bool(get(name)) for name in names}
