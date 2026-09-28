"""
test_url_guard.py — tests for the SSRF guard.

These are the tests that matter most in this project. The bot fetches a URL
from a stranger, so the failure being tested against is not "wrong output" but
"the bot reads somebody's cloud credentials and mails them over Telegram".

Run with:  python -m pytest test_url_guard.py -v
"""

from __future__ import annotations

import socket

import pytest

import url_guard
from url_guard import UnsafeURL, canonical, normalise, same_host, validate


class _FakeDNS:
    """Stand in for getaddrinfo so tests never touch the network.

    Real DNS in a test suite means flaky tests and an accidental dependency on
    the machine's resolver. The mapping is explicit, so a test says exactly
    which address a hostname is pretending to be.
    """

    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping

    def __call__(self, host, port, *args, **kwargs):
        if host not in self.mapping:
            raise socket.gaierror(-2, "Name or service not known")
        out = []
        for addr in self.mapping[host]:
            out.append((socket.AF_INET if ":" not in addr else socket.AF_INET6,
                        socket.SOCK_STREAM, 6, "", (addr, port or 0)))
        return out


@pytest.fixture
def public_dns(monkeypatch):
    mapping = {
        "wtr-lab.example": ["93.184.216.34"],
        "cdn.wtr-lab.example": ["93.184.216.35", "93.184.216.36"],
        "localhost": ["127.0.0.1"],
        "metadata.google.internal": ["169.254.169.254"],
        "rebind.example": ["93.184.216.34", "10.0.0.5"],
        "internal.example": ["192.168.1.10"],
    }
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", _FakeDNS(mapping))
    return mapping


# --- schemes ---------------------------------------------------------------

def test_rejects_file_scheme():
    with pytest.raises(UnsafeURL) as e:
        validate("file:///etc/passwd")
    assert "not supported" in str(e.value)


def test_rejects_ftp_scheme():
    with pytest.raises(UnsafeURL):
        validate("ftp://wtr-lab.example/chapter")


def test_rejects_missing_scheme():
    # Guessing http here would let a bare internal hostname slip through.
    with pytest.raises(UnsafeURL) as e:
        validate("wtr-lab.example/chapter")
    assert "http://" in str(e.value)


def test_accepts_http_and_https(public_dns):
    for url in ("http://wtr-lab.example/ch1", "https://wtr-lab.example/ch1"):
        assert validate(url).scheme in ("http", "https")


def test_upgrades_protocol_relative():
    assert normalise("//wtr-lab.example/ch1") == "https://wtr-lab.example/ch1"


# --- the SSRF cases that actually matter -----------------------------------

def test_blocks_loopback(public_dns):
    with pytest.raises(UnsafeURL) as e:
        validate("http://localhost/admin")
    assert "private or reserved" in str(e.value)


def test_blocks_cloud_metadata_endpoint(public_dns):
    """The single most valuable test in this file.

    169.254.169.254 is how a cloud instance is asked for its own credentials.
    If this regresses, the bot becomes a credential exfiltration service.
    """
    with pytest.raises(UnsafeURL):
        validate("http://169.254.169.254/latest/meta-data/iam/security-credentials/")


def test_blocks_metadata_by_hostname(public_dns):
    with pytest.raises(UnsafeURL):
        validate("http://metadata.google.internal/computeMetadata/v1/")


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.1.2.3",      # loopback
    "10.0.0.1", "172.16.0.1", "172.31.255.255",  # private
    "192.168.1.1",                 # private
    "169.254.169.254",             # link-local
    "0.0.0.0",                     # unspecified
])
def test_blocks_reserved_literals(ip):
    with pytest.raises(UnsafeURL):
        validate(f"http://{ip}/")


def test_blocks_ipv6_loopback():
    with pytest.raises(UnsafeURL):
        validate("http://[::1]:8080/")


def test_blocks_ipv6_ula():
    # fc00::/7 is private in v6 exactly as 10/8 is in v4.
    with pytest.raises(UnsafeURL):
        validate("http://[fd00::1]/")


def test_blocks_rebinding_mixed_answer(public_dns):
    """One public and one private answer means the name is not trustworthy.

    Taking the first result is the bug this test exists to prevent.
    """
    with pytest.raises(UnsafeURL) as e:
        validate("http://rebind.example/ch1")
    assert "private or reserved" in str(e.value)


def test_accepts_multi_public_answer(public_dns):
    r = validate("http://cdn.wtr-lab.example/ch1")
    assert r.ip in ("93.184.216.35", "93.184.216.36")


# --- ports -----------------------------------------------------------------

def test_blocks_ssh_port():
    with pytest.raises(UnsafeURL) as e:
        validate("http://wtr-lab.example:22/")
    assert "port 22" in str(e.value)


def test_blocks_redis_port():
    with pytest.raises(UnsafeURL):
        validate("http://wtr-lab.example:6379/")


def test_allows_unusual_but_public_port(public_dns):
    r = validate("http://wtr-lab.example:8080/ch1")
    assert r.port == 8080


def test_default_ports(public_dns):
    assert validate("https://wtr-lab.example/").port == 443
    assert validate("http://wtr-lab.example/").port == 80


# --- input handling --------------------------------------------------------

def test_rejects_empty():
    for bad in ("", "   ", None):
        with pytest.raises(UnsafeURL):
            validate(bad)


def test_rejects_overlong_url():
    with pytest.raises(UnsafeURL) as e:
        validate("http://wtr-lab.example/" + "a" * 3000)
    assert "too long" in str(e.value)


def test_strips_surrounding_whitespace(public_dns):
    assert validate("  http://wtr-lab.example/ch1  ").url.startswith("http://")


def test_unresolvable_host(public_dns):
    with pytest.raises(UnsafeURL) as e:
        validate("http://does-not-exist.example/")
    assert "could not resolve" in str(e.value)


# --- helpers ---------------------------------------------------------------

def test_canonical_drops_query_and_credentials(public_dns):
    url = "https://user:secret@wtr-lab.example/ch1?token=abc123#frag"
    got = canonical(url)
    assert "secret" not in got
    assert "token=abc123" not in got
    assert got == "https://wtr-lab.example/ch1"


def test_canonical_keeps_meaningful_port(public_dns):
    assert canonical("http://wtr-lab.example:8080/x").endswith(":8080/x")


def test_same_host_case_insensitive(public_dns):
    assert same_host("http://WTR-Lab.Example/a", "http://wtr-lab.example/b")
    assert not same_host("http://a.example/x", "http://b.example/x")
    assert not same_host("garbage", "http://wtr-lab.example/")  # must not raise


def test_validate_returns_pinned_ip(public_dns):
    r = validate("http://wtr-lab.example/ch1")
    assert r.ip == "93.184.216.34"
    assert r.host == "wtr-lab.example"
