"""Who is asking, and whether a change really comes from the panel's own page.

`trusted_header`: a proxy you control signs people in and passes their name and groups in
headers. The headers are only as trustworthy as that proxy: nothing else may reach the panel's
port. A request without the user header, or without an allowed group, is refused.

`none`: no sign-in, accepted only in DEMO mode.

Every change (POST, PUT, PATCH, DELETE) also needs a CSRF token: an HMAC of the user's name
with the panel's key, sent as a form field or the X-CSRF-Token header. The key is kept in the
state directory, so pages that are open survive a restart of the panel.
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from aipanel.config.site import AuthConfig
from aipanel.files import atomic_write

log = logging.getLogger("aipanel.auth")
KEY_BYTES = 32

CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf"


@dataclass(frozen=True)
class User:
    name: str
    groups: tuple[str, ...] = ()


class AuthError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def _from_trusted_proxy(client: str | None, proxies: list[str]) -> bool:
    if not proxies:
        return True
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(client)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(p, strict=False) for p in proxies)


def authenticate(
    headers: dict[str, str], client: str | None, config: AuthConfig, demo: bool
) -> User:
    """`headers` with lower-case names, as the server gives them."""
    if config.mode == "none":
        if not demo:  # the loader refuses this too; a second lock on the same door
            raise AuthError(403, "auth.mode none is accepted only in DEMO mode")
        return User("demo", ("demo",))
    if not _from_trusted_proxy(client, config.trusted_proxies):
        raise AuthError(403, "not from a trusted proxy")
    name = headers.get(config.user_header.lower(), "").strip()
    if not name:
        raise AuthError(401, f"no {config.user_header} header: sign in through the proxy")
    raw = headers.get(config.groups_header.lower(), "")
    groups = tuple(g.strip() for g in raw.split(config.groups_separator) if g.strip())
    if not set(groups) & set(config.allowed_groups):
        raise AuthError(403, f"{name} is not in an allowed group")
    return User(name, groups)


def load_csrf_key(path: Path) -> bytes:
    """The CSRF key from `path` (mode 0600), made there on the first start.

    Without a writable state directory the key lives in memory only: it still protects every
    change, but open pages need a reload after the panel restarts.
    """
    try:
        key = bytes.fromhex(path.read_text().strip())
        if len(key) >= KEY_BYTES:
            if path.stat().st_mode & 0o077:
                path.chmod(0o600)
            return key
        log.warning("CSRF key %s is too short: making a new one", path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("CSRF key %s unreadable (%s): making a new one", path, e)
    key = secrets.token_bytes(KEY_BYTES)
    try:
        atomic_write(path, key.hex() + "\n")  # mkstemp makes the file 0600
        os.chmod(path, 0o600)
    except OSError as e:
        log.warning(
            "CSRF key not saved (%s): kept in memory, so open pages need a reload after a restart",
            e,
        )
    return key


def csrf_token(key: bytes, user: User) -> str:
    return hmac.new(key, user.name.encode(), hashlib.sha256).hexdigest()


def csrf_ok(key: bytes, user: User, token: str | None) -> bool:
    return token is not None and hmac.compare_digest(csrf_token(key, user), token)
