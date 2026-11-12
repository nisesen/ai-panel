"""Who is asking, and whether a change really comes from the panel's own page.

`trusted_header`: a proxy you control signs people in and passes their name and groups in
headers. The headers are only as trustworthy as that proxy: nothing else may reach the panel's
port. A request without the user header, or without an allowed group, is refused.

`none`: no sign-in, accepted only in DEMO mode.

Every change (POST, PUT, PATCH, DELETE) also needs a CSRF token: an HMAC of the user's name
with a key made at startup, sent as a form field or the X-CSRF-Token header.
"""

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass

from aipanel.config.site import AuthConfig

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


def csrf_token(key: bytes, user: User) -> str:
    return hmac.new(key, user.name.encode(), hashlib.sha256).hexdigest()


def csrf_ok(key: bytes, user: User, token: str | None) -> bool:
    return token is not None and hmac.compare_digest(csrf_token(key, user), token)
