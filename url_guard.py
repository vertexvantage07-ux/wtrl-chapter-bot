"""
url_guard.py — decide whether a URL is safe for the bot to fetch.

This module exists because the bot accepts a URL from a stranger and fetches
it. That is a server-side request forgery gadget by construction: a user can
hand the bot ``http://169.254.169.254/latest/meta-data/`` and, if the bot runs
on a VPS with cloud credentials, read them out through the text file it sends
back.

So validation is not a nicety here. It is the difference between a bot and a
credential-exfiltration service with a text file attached.

Three rules, in order:

1. Scheme must be http or https. No ``file://``, no ``gopher://``, no ``ftp://``.
2. The host must not resolve to a private, loopback, link-local, or otherwise
   reserved address. Both IPv4 and IPv6 are checked, because ``[::1]`` is just
   as useful to an attacker as ``127.0.0.1``.
3. Nothing is followed automatically. A public URL that 302s to
   ``http://10.0.0.1/`` is the standard bypass, so redirects are resolved by us
   and re-validated at every hop.

The hostname is resolved here rather than in the fetcher because DNS results
change between the check and the connection. We resolve once, pin the result,
and connect to the address we validated. There is still a small TOCTOU window
between validate() and the actual connect if the caller ignores ``resolved``;
``safe_get()`` in fetcher.py does not ignore it.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Ports that exist to expose something internally. Refusing these is cheap and
# removes a whole class of "but it is a real service" mistakes.
BLOCKED_PORTS = frozenset({22, 23, 25, 445, 1433, 2375, 3306, 3389, 5432, 5672, 6379, 9200, 11211, 27017})

MAX_URL_LENGTH = 2048


class UnsafeURL(ValueError):
    """Raised when a URL must not be fetched. The message is user-facing."""


@dataclass(frozen=True)
class ResolvedURL:
    """A URL that passed validation, with the address it was checked against.

    ``ip`` is carried so the fetcher can connect to the exact address that was
    validated instead of re-resolving and hoping for the same answer.
    """

    url: str
    scheme: str
    host: str
    port: int
    ip: str


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    """True only for addresses that are genuinely routable on the public internet."""
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check_port(port: int) -> None:
    if port in BLOCKED_PORTS:
        raise UnsafeURL(
            f"port {port} is not allowed - it usually points at something internal"
        )


def _resolve(host: str) -> str:
    """Resolve a host to a single public IP, or refuse.

    Every address the host resolves to is checked, not just the first one. A
    name that returns one public and one private address is a rebinding attempt,
    and taking the first result is how it gets through.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"could not resolve '{host}': {exc.strerror or exc}") from exc

    if not infos:
        raise UnsafeURL(f"could not resolve '{host}'")

    checked: set[str] = set()
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:  # pragma: no cover - getaddrinfo shape changed
            raise UnsafeURL(f"unparseable address for '{host}'") from exc
        if not _is_public_ip(ip):
            raise UnsafeURL(
                f"'{host}' points at a private or reserved address ({addr}). "
                "This bot only fetches public pages."
            )
        checked.add(addr)

    # Return the first address; all of them have been proven public.
    return sorted(checked)[0]


def normalise(raw: str) -> str:
    """Trim and length-check a user-supplied URL without validating it yet."""
    if raw is None:
        raise UnsafeURL("no URL was given")
    url = raw.strip()
    if not url:
        raise UnsafeURL("no URL was given")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURL(f"URL is too long (limit {MAX_URL_LENGTH} characters)")
    if url.startswith("//"):
        # A protocol-relative URL pasted from a browser. Assume https, which is
        # what the user's browser would have used.
        url = "https:" + url
    if "://" not in url:
        # Bare host with no scheme. Refuse rather than guess: guessing http lets
        # a user smuggle a local hostname past a naive check.
        raise UnsafeURL("include http:// or https:// in the URL")
    return url


def validate(raw: str) -> ResolvedURL:
    """Validate a URL for fetching. Raises UnsafeURL with a user-facing reason."""
    url = normalise(raw)

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"'{scheme}://' URLs are not supported, use http or https")

    host = parts.hostname
    if not host:
        raise UnsafeURL("that URL has no host in it")

    port = parts.port or (443 if scheme == "https" else 80)
    _check_port(port)

    # A bare IPv6 literal arrives with brackets; ipaddress wants them gone.
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
        host = literal
    except ValueError:
        pass  # a real hostname, which _resolve will handle

    ip = _resolve(host)
    return ResolvedURL(url=url, scheme=scheme, host=host, port=port, ip=ip)


def same_host(a: str, b: str, limit: int = 5) -> bool:
    """True when two URLs point at the same host, for de-duplicating a batch."""
    try:
        ha = urlsplit(normalise(a)).hostname or ""
        hb = urlsplit(normalise(b)).hostname or ""
    except UnsafeURL:
        return False
    return ha.lower().rstrip(".") == hb.lower().rstrip(".")


def canonical(url: str) -> str:
    """Return a stable form for logging, without any userinfo that may be present.

    The log line is what a human reads when a user reports 'it did not work', so
    it needs to be recognisable. It is not a place to write a password the user
    pasted into the URL field.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme, netloc, path, "", ""))
