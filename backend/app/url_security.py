"""
url_security.py — SSRF protection for external URL validation.

Shared validator used at webhook config-write time and at dispatch time.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Optional, Tuple


def _check_ip_address_safe(addr: str) -> Tuple[bool, str]:
    """Return (True, "") if addr is a safe public IP, else (False, reason)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False, f"invalid IP address: {addr!r}"

    # Evaluate mapped IPv4 addresses using their actual destination. Older
    # Python versions can otherwise classify mapped shared-space addresses as
    # global, and mapped multicast addresses do not set IPv6 is_multicast.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.is_loopback:
        return False, f"loopback address blocked: {addr}"
    if ip.is_link_local:
        return False, f"link-local/metadata address blocked: {addr}"
    # Catch 0.0.0.0 and ::
    if ip == ipaddress.ip_address("0.0.0.0") or ip == ipaddress.ip_address("::"):
        return False, f"unspecified address blocked: {addr}"
    if ip.is_private:
        return False, f"private RFC1918/RFC4193 address blocked: {addr}"
    # Python < 3.11 is_private may not cover fd00::/8 (RFC4193 unique-local)
    if ip.version == 6 and (int(ip) >> 120) == 0xFD:
        return False, f"unique-local IPv6 address blocked: {addr}"
    if ip.is_multicast:
        return False, f"multicast address blocked: {addr}"
    # RFC6598 shared address space (100.64.0.0/10) is neither private nor
    # global. Requiring global unicast also excludes other nonpublic ranges.
    if not ip.is_global:
        return False, f"non-global address blocked: {addr}"

    return True, ""


def _resolve_safe_external_url(url: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate that *url* is safe to use as an external webhook destination,
    AND return the first resolved IP for use as a connection pin.

    Checks:
      1. Scheme must be http or https.
      2. Hostname must be present.
      3. All resolved IPs must be global unicast (blocks loopback, link-local,
         metadata, private/shared ranges, multicast, and unspecified addresses).

    Returns (True, "", resolved_ip) when safe, or (False, reason, None) when blocked.

    The returned IP can be substituted into the URL at dispatch time (with the
    original Host header and TLS SNI preserved) so the actual TCP connection
    targets the validated address — defeating DNS rebinding TOCTOU attacks
    where a hostname's A-record flips between validation and connect.

    Performs a blocking DNS lookup via socket.getaddrinfo; run in an executor
    when calling from async code.
    """
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme must be http or https, got {parsed.scheme!r}", None

    hostname = parsed.hostname
    if not hostname:
        return False, "missing hostname", None

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}", None

    if not infos:
        return False, "DNS returned no addresses", None

    first_safe_ip: Optional[str] = None
    for info in infos:
        resolved_addr = info[4][0]
        safe, reason = _check_ip_address_safe(resolved_addr)
        if not safe:
            return False, reason, None
        if first_safe_ip is None:
            first_safe_ip = resolved_addr

    return True, "", first_safe_ip


def _is_safe_external_url(url: str) -> Tuple[bool, str]:
    """Backward-compatible wrapper returning just (safe, reason).

    Use _resolve_safe_external_url() directly when you also need the
    resolved IP for connection pinning.
    """
    safe, reason, _ = _resolve_safe_external_url(url)
    return safe, reason
