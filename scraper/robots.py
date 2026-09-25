"""robots.txt: fetched once per run, then every URL (home page included) is checked against it.

Why not `urllib.robotparser`: it picks the group by the text of the *full* user agent before the
first "/", which for a browser-like user agent is "Mozilla", so a group written for this tool was
silently ignored. It also applies the first matching rule instead of the most specific one.

The matcher below follows RFC 9309:
- the group is chosen by the product token (`playwright-scraper-demo`, case-insensitive); groups
  with the same token are merged; without one, the `*` group applies; without both, nothing does
- inside the group the most specific rule wins (longest pattern); on a tie, allow wins
- `*` matches any sequence of characters and a trailing `$` anchors the end of the path
- `/robots.txt` itself is always allowed
- 4xx = no robots.txt (crawl allowed); 5xx, 429 or a network error = unreachable (do not crawl)

`Crawl-delay` is not part of RFC 9309 but is widely used; it is read from the same group.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

from playwright.async_api import APIRequestContext

from scraper import TOOL_NAME

# RFC 9309 product token: letters, "-" and "_". Anything after it ("/1.0", comments) is ignored.
_TOKEN_RE = re.compile(r"[A-Za-z_-]+")


class RobotsUnavailable(RuntimeError):
    """robots.txt could not be fetched (5xx / 429 / network). RFC 9309 2.3.1.4: assume complete disallow."""


class RobotsDisallowed(RuntimeError):
    """robots.txt disallows a URL the run cannot do without (the home page with the category list)."""


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (is_allow, path pattern)
    crawl_delay: float | None = None
    has_body: bool = False  # a user-agent line after a rule starts a new group


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class RobotsRules:
    """The rules that apply to one product token, already merged across groups."""

    group: str  # the product token, "*" or "none" (no group applies)
    rules: tuple[Rule, ...]
    crawl_delay: float | None

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{unquote(parts.query)}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best_len, allowed = -1, True  # no matching rule -> allowed
        for rule in self.rules:
            if rule.regex.match(path):
                n = len(rule.pattern)
                if n > best_len or (n == best_len and rule.allow):
                    best_len, allowed = n, rule.allow
        return allowed


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = unquote(pattern[:-1] if anchored else pattern)
    return re.compile(".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else ""))


def _agent_token(value: str) -> str:
    if value.strip().startswith("*"):
        return "*"
    m = _TOKEN_RE.match(value.strip())
    return m.group(0).lower() if m else ""


def parse_robots(text: str, product_token: str = TOOL_NAME) -> RobotsRules:
    groups: list[_Group] = []
    current: _Group | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or current.has_body:
                current = _Group()
                groups.append(current)
            current.agents.append(_agent_token(value))
        elif current is None:
            continue  # rules before the first user-agent line belong to no group
        elif key in ("allow", "disallow"):
            current.has_body = True
            if value:  # an empty "Disallow:" matches nothing
                current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            current.has_body = True
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass

    token = product_token.lower()
    chosen = [g for g in groups if token in g.agents]
    label = product_token
    if not chosen:
        chosen = [g for g in groups if "*" in g.agents]
        label = "*" if chosen else "none"
    rules = tuple(Rule(allow, pat, _compile(pat)) for g in chosen for allow, pat in g.rules)
    delays = [g.crawl_delay for g in chosen if g.crawl_delay]
    return RobotsRules(label, rules, max(delays) if delays else None)


@dataclass
class RobotsPolicy:
    url: str
    http_status: int
    summary: str
    rules: RobotsRules | None = None  # None: no robots.txt (4xx) -> everything is allowed
    product_token: str = TOOL_NAME

    @property
    def crawl_delay_s(self) -> float | None:
        return self.rules.crawl_delay if self.rules else None

    @property
    def group(self) -> str:
        return self.rules.group if self.rules else "none"

    def allows(self, url: str) -> bool:
        return True if self.rules is None else self.rules.allows(url)

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "http_status": self.http_status,
            "summary": self.summary,
            "product_token": self.product_token,
            "group_applied": self.group,
            "rules": len(self.rules.rules) if self.rules else 0,
            "crawl_delay_s": self.crawl_delay_s,
        }


async def fetch_robots(request: APIRequestContext, base_url: str,
                       product_token: str = TOOL_NAME) -> RobotsPolicy:
    url = urljoin(base_url, "/robots.txt")
    try:
        resp = await request.get(url, timeout=15_000)
        status = resp.status
        body = await resp.text() if 200 <= status < 300 else ""
    except Exception as exc:  # network error -> be conservative
        raise RobotsUnavailable(f"{url}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc

    if 200 <= status < 300:
        rules = parse_robots(body, product_token)
        if rules.group == "none":
            summary = "robots.txt found; no group for this tool or '*': no path restrictions"
        else:
            delay = f", Crawl-delay {rules.crawl_delay:g} s" if rules.crawl_delay else ""
            summary = (f"robots.txt found; group '{rules.group}' applies ({len(rules.rules)} rules{delay}), "
                       "checked for every URL")
        return RobotsPolicy(url, status, summary, rules, product_token)
    if status == 429 or status >= 500:
        raise RobotsUnavailable(f"{url}: HTTP {status}")
    if 400 <= status < 500:
        return RobotsPolicy(url, status, f"no robots.txt (HTTP {status}): no path restrictions (RFC 9309 2.3.1.3)",
                            None, product_token)
    raise RobotsUnavailable(f"{url}: unexpected HTTP {status}")
