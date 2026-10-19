from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx


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
    def __init__(self, api_key: str):
        self.api_key = api_key

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
        return self._post("search", {"query": query, "max_results": 5})["results"]

    def read(self, url: str) -> str:
        results = self._post("extract", {"urls": [url]})["results"]
        if not results:
            raise ValueError("Extraction returned no content")
        return results[0]["raw_content"]
