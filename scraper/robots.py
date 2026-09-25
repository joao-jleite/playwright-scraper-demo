"""robots.txt check done before any crawling (RFC 9309 semantics)."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from playwright.async_api import APIRequestContext


class RobotsUnavailable(RuntimeError):
    """robots.txt could not be fetched (5xx / network). RFC 9309 says: assume full disallow."""


@dataclass
class RobotsPolicy:
    url: str
    http_status: int
    summary: str
    crawl_delay_s: float | None = None
    _parser: RobotFileParser | None = field(default=None, repr=False)

    def allows(self, url: str, user_agent: str) -> bool:
        # No parser means robots.txt was absent (4xx) -> everything is allowed.
        return True if self._parser is None else self._parser.can_fetch(user_agent, url)

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "http_status": self.http_status,
            "summary": self.summary,
            "crawl_delay_s": self.crawl_delay_s,
        }


async def fetch_robots(request: APIRequestContext, base_url: str, user_agent: str) -> RobotsPolicy:
    url = urljoin(base_url, "/robots.txt")
    try:
        resp = await request.get(url, timeout=15_000)
    except Exception as exc:  # network error -> be conservative
        raise RobotsUnavailable(f"{url}: {exc}") from exc

    status = resp.status
    if status == 200:
        parser = RobotFileParser()
        parser.parse((await resp.text()).splitlines())
        delay = parser.crawl_delay(user_agent)
        return RobotsPolicy(url, status, "robots.txt found and parsed; rules enforced per URL",
                            float(delay) if delay else None, parser)
    if 400 <= status < 500:
        return RobotsPolicy(url, status, f"no robots.txt (HTTP {status}): no path restrictions (RFC 9309 2.3.1.3)")
    raise RobotsUnavailable(f"{url}: HTTP {status}")
