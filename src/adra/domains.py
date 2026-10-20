"""Exact hostname allowlists shared by tools, execution and report validation."""

import ipaddress
import re
from urllib.parse import urlsplit, unquote


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
    def __init__(self, domains=(), url_prefixes=()):
        self.domains = tuple(dict.fromkeys(normalize_domain(d) for d in domains))
        self.url_prefixes = tuple(url_prefixes)
        for prefix in self.url_prefixes:
            p = urlsplit(prefix)
            if (not DomainPolicy(self.domains).allows(prefix) or p.query or p.fragment
                    or not p.path.startswith("/") or "%" in p.path
                    or any(s in (".", "..") for s in p.path.split("/"))):
                raise ValueError("Invalid allowed URL prefix")
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
            if self.domains and normalize_domain(parts.hostname) not in self.domains:
                return False
            if self.url_prefixes:
                path = unquote(parts.path)
                if "%" in path or "\\" in path or any(s in (".", "..") for s in path.split("/")):
                    return False
                return any(parts.scheme == p.scheme and parts.hostname == p.hostname
                           and parts.port == p.port
                           and (path == p.path.rstrip("/") or
                                path.startswith(p.path.rstrip("/") + "/"))
                           for p in map(urlsplit, self.url_prefixes))
            return True
        except (ValueError, UnicodeError, TypeError):
            return False

    def require(self, url: str) -> None:
        if not self.allows(url):
            raise SourceDomainError("Source URL is invalid or outside allowed domains")
