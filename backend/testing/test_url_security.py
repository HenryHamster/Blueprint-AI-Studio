"""Tests for url_security._is_safe_external_url SSRF validation."""

import socket
from unittest.mock import patch

import pytest

from app.url_security import (
    _check_ip_address_safe,
    _is_safe_external_url,
    _resolve_safe_external_url,
)


def _mock_getaddrinfo(ip: str):
    """Return a socket.getaddrinfo-compatible result for a single IP address."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]


def _mock_getaddrinfo_v6(ip: str):
    return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ip, 0, 0, 0))]


# ---------------------------------------------------------------------------
# _check_ip_address_safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr,expected_safe",
    [
        ("127.0.0.1", False),
        ("127.0.0.2", False),
        ("::1", False),
        ("169.254.169.254", False),  # AWS metadata
        ("169.254.0.1", False),  # link-local
        ("10.0.0.1", False),  # RFC1918
        ("10.255.255.255", False),  # RFC1918
        ("172.16.0.1", False),  # RFC1918
        ("192.168.1.1", False),  # RFC1918
        ("0.0.0.0", False),
        ("::", False),
        ("fd00::1", False),  # RFC4193 unique-local
        ("fd12:3456:789a::1", False),  # RFC4193 unique-local (fd00::/8)
        ("100.64.0.1", False),  # RFC6598 shared address space
        ("100.127.255.254", False),  # upper end of RFC6598
        ("224.0.0.1", False),  # IPv4 multicast
        ("239.255.255.250", False),  # administratively scoped multicast
        ("ff02::1", False),  # IPv6 link-local multicast
        ("ff0e::1", False),  # IPv6 global-scope multicast
        ("::ffff:100.64.0.1", False),  # IPv4-mapped shared address
        ("::ffff:224.0.0.1", False),  # IPv4-mapped multicast
        ("::ffff:8.8.8.8", True),  # IPv4-mapped public unicast
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),  # Cloudflare public IPv6
    ],
)
def test_check_ip_address_safe(addr, expected_safe):
    safe, reason = _check_ip_address_safe(addr)
    assert safe is expected_safe, (
        f"{addr!r}: expected safe={expected_safe}, got safe={safe}, reason={reason!r}"
    )


# ---------------------------------------------------------------------------
# _is_safe_external_url — scheme validation (no DNS needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "//example.com/hook",
        "",
        "notaurl",
    ],
)
def test_rejects_bad_scheme(url):
    safe, reason = _is_safe_external_url(url)
    assert not safe
    assert reason


def test_accepts_http_scheme():
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("8.8.8.8")):
        safe, _ = _is_safe_external_url("http://example.com/webhook")
    assert safe


def test_accepts_https_scheme():
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("8.8.8.8")):
        safe, _ = _is_safe_external_url("https://example.com/webhook")
    assert safe


# ---------------------------------------------------------------------------
# _is_safe_external_url — blocked hosts (DNS mocked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "::1",
        "0.0.0.0",
        "192.168.1.100",
        "172.16.50.1",
        "fd00::1",
    ],
)
def test_rejects_private_resolved_ip(ip):
    addrinfo = _mock_getaddrinfo_v6(ip) if ":" in ip else _mock_getaddrinfo(ip)
    with patch("socket.getaddrinfo", return_value=addrinfo):
        safe, reason = _is_safe_external_url("https://internal.example.com/hook")
    assert not safe, f"Expected {ip!r} to be blocked"
    assert reason


@pytest.mark.parametrize(
    "unsafe_ip",
    [
        "100.64.0.1",
        "100.127.255.254",
        "224.0.0.1",
        "239.255.255.250",
        "ff02::1",
        "ff0e::1",
        "::ffff:100.64.0.1",
        "::ffff:224.0.0.1",
    ],
)
def test_rejects_mixed_public_and_nonpublic_dns_answers(unsafe_ip):
    unsafe_info = (
        _mock_getaddrinfo_v6(unsafe_ip)
        if ":" in unsafe_ip
        else _mock_getaddrinfo(unsafe_ip)
    )
    answers = _mock_getaddrinfo("8.8.8.8") + unsafe_info
    with patch("socket.getaddrinfo", return_value=answers):
        safe, reason, pinned_ip = _resolve_safe_external_url(
            "https://hooks.example.com/webhook"
        )
    assert not safe
    assert reason
    assert pinned_ip is None


# ---------------------------------------------------------------------------
# _is_safe_external_url — accepted public hosts (DNS mocked)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "104.16.133.229",
    ],
)
def test_accepts_public_resolved_ip(ip):
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
        safe, reason = _is_safe_external_url("https://hooks.example.com/webhook")
    assert safe, f"Expected {ip!r} to be accepted, got: {reason!r}"


def test_rejects_dns_failure():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
        safe, reason = _is_safe_external_url("https://nonexistent.invalid/hook")
    assert not safe
    assert "DNS" in reason


def test_rejects_empty_dns_result():
    with patch("socket.getaddrinfo", return_value=[]):
        safe, reason = _is_safe_external_url("https://example.com/hook")
    assert not safe
    assert "no addresses" in reason.lower()


# ---------------------------------------------------------------------------
# _resolve_safe_external_url — returns resolved IP for connection pinning
# ---------------------------------------------------------------------------


def test_resolve_returns_ip_when_safe():
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("8.8.8.8")):
        safe, reason, ip = _resolve_safe_external_url("https://example.com/hook")
    assert safe
    assert reason == ""
    assert ip == "8.8.8.8"


def test_resolve_returns_none_ip_when_blocked():
    with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
        safe, reason, ip = _resolve_safe_external_url(
            "https://internal.example.com/hook"
        )
    assert not safe
    assert reason
    assert ip is None


def test_resolve_returns_ipv6_when_safe():
    with patch(
        "socket.getaddrinfo",
        return_value=_mock_getaddrinfo_v6("2606:4700:4700::1111"),
    ):
        safe, _, ip = _resolve_safe_external_url("https://v6.example.com/hook")
    assert safe
    assert ip == "2606:4700:4700::1111"
