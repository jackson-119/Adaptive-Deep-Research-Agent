from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from adra.domains import DomainPolicy


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise ValueError("Only HTTP(S) source URLs without credentials are accepted")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/",
                       parts.query, ""))


class ResearchTools(Protocol):
    def search(self, query: str) -> list[dict]: ...
    def read(self, url: str) -> str: ...


class SnapshotTools:
    """Exact-query replay; missing queries fail rather than silently using unrelated evidence."""

    def __init__(self, snapshot: dict):
        self.snapshot = snapshot

    def search(self, query: str) -> list[dict]:
        if query not in self.snapshot["searches"]:
            raise ValueError("Query is absent from the replay snapshot")
        return self.snapshot["searches"][query]

    def read(self, url: str) -> str:
        pages = {canonical_url(k): v for k, v in self.snapshot["pages"].items()}
        if url not in pages:
            raise ValueError("Page is absent from the replay snapshot")
        return pages[url]


class TavilyTools:
    def __init__(self, api_key: str, allowed_domains=(), include_raw_content=False):
        self.api_key = api_key
        self.include_raw_content = include_raw_content
        self.domain_policy = DomainPolicy(allowed_domains)
        self.allowed_domains = self.domain_policy.domains

    def _post(self, endpoint: str, payload: dict) -> dict:
        # No implicit retries: every attempted request consumes one agent tool call.
        response = httpx.post(
            "https://api.tavily.com/" + endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def search(self, query: str) -> list[dict]:
        payload = {"query": query, "max_results": 5}
        if self.include_raw_content:
            payload["include_raw_content"] = True
        if self.allowed_domains:
            payload.update(include_domains=list(self.allowed_domains), include_domains_mode="restrict")
        # The executor rechecks every returned URL, including in snapshot mode.
        return self._post("search", payload)["results"]

    def read(self, url: str) -> str:
        self.domain_policy.require(url)
        results = self._post("extract", {"urls": [url]})["results"]
        if not results:
            raise ValueError("Extraction returned no content")
        result = results[0]
        # Fail closed in restricted mode if extraction omits its source URL.
        # A disclosed redirect to a disallowed host must not enter evidence.
        if self.allowed_domains or self.domain_policy.url_prefixes or "url" in result:
            self.domain_policy.require(result.get("url", ""))
        return result["raw_content"]
