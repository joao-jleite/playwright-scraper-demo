"""robots.txt: group selection by product token, RFC 9309 matching, fetch outcomes (no network)."""

import asyncio
import logging

import pytest

from scraper import TOOL_NAME
from scraper.crawler import USER_AGENT, Crawler, CrawlSettings
from scraper.robots import RobotsDisallowed, RobotsPolicy, RobotsUnavailable, fetch_robots, parse_robots

SITE = "https://books.toscrape.com"


def test_user_agent_names_the_tool_and_group_is_matched_on_the_token():
    assert TOOL_NAME in USER_AGENT and USER_AGENT.startswith("Mozilla/5.0")
    # The exact case urllib.robotparser got wrong: it matched the full UA as "mozilla" and used "*".
    rules = parse_robots("User-agent: playwright-scraper-demo\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert rules.group == TOOL_NAME
    assert not rules.allows(SITE + "/")
    assert not rules.allows(SITE + "/catalogue/category/books/travel_2/index.html")


def test_token_match_is_case_insensitive_and_ignores_a_version_suffix():
    rules = parse_robots("User-agent: Playwright-Scraper-Demo/1.0\nDisallow: /catalogue/\nCrawl-delay: 5\n")
    assert rules.group == TOOL_NAME and rules.crawl_delay == 5
    assert not rules.allows(SITE + "/catalogue/x.html") and rules.allows(SITE + "/")


def test_star_group_applies_when_no_group_names_the_tool():
    rules = parse_robots("User-agent: googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /private/\nCrawl-delay: 2\n")
    assert rules.group == "*" and rules.crawl_delay == 2
    assert rules.allows(SITE + "/catalogue/a.html") and not rules.allows(SITE + "/private/a.html")


def test_no_matching_group_means_no_rules():
    rules = parse_robots("User-agent: googlebot\nDisallow: /\n")
    assert rules.group == "none" and rules.allows(SITE + "/anything")


def test_longest_match_wins_and_allow_wins_a_tie():
    rules = parse_robots("User-agent: *\nAllow: /\nDisallow: /catalogue/\nAllow: /catalogue/category/\n")
    assert rules.allows(SITE + "/index.html")
    assert not rules.allows(SITE + "/catalogue/sharp-objects_997/index.html")  # urllib's first match allowed it
    assert rules.allows(SITE + "/catalogue/category/books/travel_2/index.html")
    tie = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\n")
    assert tie.allows(SITE + "/page")


def test_wildcards_end_anchor_query_and_robots_txt_itself():
    rules = parse_robots("User-agent: *\nDisallow: /\nAllow: /books\nDisallow: /*.pdf$\n"
                         "Allow: /search\nDisallow: /search?q=\n")
    assert rules.allows(SITE + "/books/page.html")           # "/books" (6 octets) beats "/" (1)
    assert not rules.allows(SITE + "/books/report.pdf")      # "/*.pdf$" (7) beats "/books" (6)
    assert rules.allows(SITE + "/books/report.pdf.html")     # "$" anchors the end
    assert rules.allows(SITE + "/search")
    assert not rules.allows(SITE + "/search?q=travel")       # the query string is part of the match
    assert not rules.allows(SITE + "/other")
    assert rules.allows(SITE + "/robots.txt")                # always allowed


def test_groups_for_the_same_token_are_merged_and_empty_disallow_is_ignored():
    text = ("User-agent: playwright-scraper-demo\nDisallow: /a/\n\n"
            "User-agent: other\nUser-agent: playwright-scraper-demo\nDisallow: /b/\nDisallow:\n")
    rules = parse_robots(text)
    assert not rules.allows(SITE + "/a/1") and not rules.allows(SITE + "/b/1") and rules.allows(SITE + "/c/1")


class _Resp:
    def __init__(self, status: int, body: str = "") -> None:
        self.status, self._body = status, body

    async def text(self) -> str:
        return self._body


class _FakeRequest:
    """Stands in for Playwright's APIRequestContext: returns a canned response or raises."""

    def __init__(self, status: int = 200, body: str = "", exc: Exception | None = None) -> None:
        self.status, self.body, self.exc, self.urls = status, body, exc, []

    async def get(self, url: str, timeout: int = 0) -> _Resp:
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return _Resp(self.status, self.body)


def _fetch(**kw) -> RobotsPolicy:
    return asyncio.run(fetch_robots(_FakeRequest(**kw), SITE + "/"))


def test_fetch_200_applies_the_tool_group_and_its_crawl_delay():
    policy = _fetch(status=200, body="User-agent: playwright-scraper-demo\nDisallow: /catalogue/\nCrawl-delay: 5\n"
                                     "User-agent: *\nAllow: /\n")
    assert policy.group == TOOL_NAME and policy.crawl_delay_s == 5
    assert not policy.allows(SITE + "/catalogue/category/books/travel_2/index.html")
    assert policy.as_dict()["group_applied"] == TOOL_NAME and "Crawl-delay 5 s" in policy.summary


def test_fetch_404_means_no_restrictions():
    policy = _fetch(status=404)
    assert policy.rules is None and policy.allows(SITE + "/anything") and policy.crawl_delay_s is None


@pytest.mark.parametrize("status", [500, 503, 429])
def test_fetch_5xx_or_429_refuses_to_crawl(status):
    with pytest.raises(RobotsUnavailable, match=f"HTTP {status}"):
        _fetch(status=status)


def test_fetch_network_error_refuses_to_crawl():
    with pytest.raises(RobotsUnavailable, match="connection reset"):
        _fetch(exc=RuntimeError("net::ERR_CONNECTION_RESET connection reset"))


def test_home_page_is_checked_before_it_is_loaded():
    policy = RobotsPolicy(SITE + "/robots.txt", 200, "test", parse_robots("User-agent: *\nDisallow: /\n"))
    # context=None: the check must fail before any page is opened
    crawler = Crawler(None, CrawlSettings(), logging.getLogger("test"), robots=policy)  # type: ignore[arg-type]
    with pytest.raises(RobotsDisallowed, match="disallows"):
        asyncio.run(crawler.discover_categories())
