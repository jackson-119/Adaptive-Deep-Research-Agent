"""Exact hostname allowlists shared by tools, execution and report validation."""

import ipaddress
import re
from urllib.parse import urlsplit


class SourceDomainError(ValueError):
    """A source is outside the configured hostname allowlist."""


def normalize_domain(value: str) -> str:
    value = value.strip().removesuffix(".")
    try:
        host = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Invalid domain hostname") from None
    labels = host.split(".")
    if (len(host) > 253 or len(labels) < 2 or
            any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                for p in labels)):
        raise ValueError("Allowed domains must be hostnames, without scheme, path, port or wildcard")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("Use a domain hostname, not an IP address")


class DomainPolicy:
    def __init__(self, domains=()):
        self.domains = tuple(dict.fromkeys(normalize_domain(d) for d in domains))
        if len(self.domains) > 300:
            raise ValueError("At most 300 allowed domains are supported")

    def allows(self, url: str) -> bool:
        try:
            if any(ord(c) < 33 for c in url) or "\\" in url:
                return False
            parts = urlsplit(url)
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None):
                return False
            _ = parts.port  # Reject malformed/out-of-range ports.
            if not self.domains:
                return True
            return normalize_domain(parts.hostname) in self.domains
        except (ValueError, UnicodeError, TypeError):
            return False

    def require(self, url: str) -> None:
        if not self.allows(url):
            raise SourceDomainError("Source URL is invalid or outside allowed domains")
