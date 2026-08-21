"""
job_source.career_page
~~~~~~~~~~~~~~~~~~~~~~~

Given a company's official website, finds their job LISTINGS page — the
page where their current open positions are shown (either on their own
domain, or on a third-party ATS platform they use, e.g.
jobs.ashbyhq.com/harvey). This matches the assignment's own example:

    input:  https://www.linkedin.com/jobs/view/4427787182/
    output: https://jobs.ashbyhq.com/harvey?utm_source=58AzKxpoq0

NOTE: this deliberately stops at the LISTINGS page — it does not drill
down into one specific job posting. An earlier version of this module did
that; it was simplified after the assignment brief clarified the goal is
"get to job listing page of individual company's website", not one
specific opening.

Strategy (fastest/most reliable first, most expensive/general last):
  A. ATS detection      — if the site links to a known applicant-tracking
                           platform (Greenhouse, Lever, Ashby, Workday,
                           etc.), that link IS the listings page — return
                           it directly, no further hops needed.
  B. Heuristic scan      — look for common careers-link text/paths/
                           subdomains on the homepage.
  C. LLM-guided fallback — for sites that don't match A or B, hand the
                           page's links to an LLM and ask it to pick the
                           careers/jobs link. Slow path, used only when
                           heuristics fail.

Typical usage
-------------
    from job_source.career_page import find_job_listing_page

    result = find_job_listing_page("https://www.acmecorp.com")
    print(result.listing_url, result.method)
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CareerPageError(Exception):
    """Base class for all errors in this module."""


class FetchError(CareerPageError):
    """Raised when a page could not be fetched at all."""


class CareerPageNotFoundError(CareerPageError):
    """Raised when no plausible job listings page could be located."""


@dataclass(frozen=True)
class ListingResult:
    listing_url: str
    method: str
    """Which strategy resolved this result: 'ats', 'heuristic', or 'llm'."""


# ---------------------------------------------------------------------------
# Known ATS platforms — a link to any of these domains IS the listings page.
# ---------------------------------------------------------------------------
_ATS_DOMAINS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co",
    "jobs.ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "workable.com", "breezy.hr",
    "recruitee.com", "bamboohr.com",
}

_CAREER_LINK_HINTS = re.compile(
    r"career|job|hiring|join[\s-]?us|join[\s-]?our|work[\s-]?with[\s-]?us|"
    r"open[\s-]?position|vacanc|opportunit",
    re.IGNORECASE,
)

# Stronger, less ambiguous hiring-specific phrases — worth more than a bare
# "career" match, which collides with plenty of non-hiring uses of that word
# (e.g. an ed-tech/coaching company's own "Career Tracks" product pages).
_STRONG_HIRE_HINTS = re.compile(
    r"\bjob(s)?\b|\bhiring\b|\bvacanc|open[\s-]?position|"
    r"current[\s-]?opening|join[\s-]?our[\s-]?team|work[\s-]?with[\s-]?us|"
    r"we'?re[\s-]?hiring",
    re.IGNORECASE,
)
# NOTE: "opportunit" deliberately excluded from strong hints — it's far too
# common in general marketing/business copy ("$4.5T AI opportunity gap")
# to reliably signal a hiring page on its own. It's still in the weak
# _CAREER_LINK_HINTS above, worth 1 point rather than 3.

# A careers./jobs. label anywhere in the domain is one of the strongest,
# least ambiguous signals available — stronger than any text/path keyword
# match. Matches both "careers.company.com" AND "apply.careers.company.com"
# (a common pattern: a landing subdomain that itself has "careers." as a
# middle label, one hop before the real listings).
_CAREER_SUBDOMAIN_HINT = re.compile(r"(^|\.)(careers?|jobs?)\.", re.IGNORECASE)

# Press/news/blog subdomains are unlikely to BE the careers hub, even if a
# headline happens to contain a hint word — small penalty to help break ties
# away from this class of page.
_NON_CAREER_SUBDOMAIN_HINT = re.compile(r"^(news|blog|press|insights)\.", re.IGNORECASE)

# Terms that commonly collide with a bare "career" match on marketing/
# product pages that have nothing to do with hiring at the company itself.
_CAREER_FALSE_POSITIVE_HINTS = re.compile(
    r"\btrack(s)?\b|\bcourse(s)?\b|\bprogram(s)?\b|\bcurriculum\b|"
    r"\bbootcamp\b|\bcertificat|\blearn\b|\bcounsel",
    re.IGNORECASE,
)

_LISTING_ONLY_HINTS = re.compile(
    r"^/(careers?|jobs?|hiring)(/(careers?|jobs?|openings?|positions?|listings?))?/?$",
    re.IGNORECASE,
)

# Explicit "search/view jobs" call-to-action phrasing — the strongest
# possible signal that a link leads to the ACTUAL listings, as distinct
# from a careers landing/intro page. E.g. many company career pages are a
# landing page (mission, culture, benefits) with a "Search Jobs" or "View
# Open Roles" button/link that leads to a DIFFERENT, deeper URL holding
# the real listings (e.g. spacex.com/careers -> spacex.com/careers/jobs).
_JOBS_SUBPAGE_HINT = re.compile(
    r"\bfind\s*jobs?\b|\bsearch\s*jobs?\b|\bview\s*(all\s*)?(jobs?|openings?|positions?)\b|"
    r"\bbrowse\s*(jobs?|openings?)\b|\bopen\s*(roles?|positions?)\b|"
    r"\ball\s*(jobs?|openings?)\b",
    re.IGNORECASE,
)

# External job boards/aggregators — even if a company's homepage links out
# to one of these, picking it as "the listings page" defeats the goal of a
# direct path to the company's own job listings.
_EXTERNAL_JOB_BOARD_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "naukri.com",
    "monster.com", "ziprecruiter.com", "simplyhired.com", "wellfound.com",
    "angel.co",
}


def _is_external_job_board(url: str) -> bool:
    domain = _domain(url)
    return any(domain == d or domain.endswith("." + d) for d in _EXTERNAL_JOB_BOARD_DOMAINS)


# A link to an ATS platform might point to one SPECIFIC job posting rather
# than the general listings board — e.g. a page with hundreds of job cards
# might have its FIRST such link happen to be one particular opening
# (boards.greenhouse.io/spacex/jobs/8557110002) rather than the board root
# (boards.greenhouse.io/spacex). The assignment wants the LISTINGS page,
# not one individual opening, so any such link is normalized back to its
# board root before being returned.
_ATS_JOB_ID_STRIP_PATTERNS = [
    (re.compile(r"^(https?://boards\.greenhouse\.io/[^/]+)/jobs/\d+.*", re.IGNORECASE), r"\1"),
    (re.compile(r"^(https?://job-boards\.greenhouse\.io/[^/]+)/jobs/\d+.*", re.IGNORECASE), r"\1"),
    (re.compile(r"^(https?://jobs\.lever\.co/[^/]+)/[a-f0-9-]{20,}.*", re.IGNORECASE), r"\1"),
    (re.compile(r"^(https?://jobs\.ashbyhq\.com/[^/]+)/[a-f0-9-]{20,}.*", re.IGNORECASE), r"\1"),
]


def _normalize_ats_url(url: str) -> str:
    for pattern, replacement in _ATS_JOB_ID_STRIP_PATTERNS:
        if pattern.match(url):
            return pattern.sub(replacement, url)
    return url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_MAX_HOPS = 3  # homepage -> careers landing -> real listings page, with headroom


def _looks_like_final_listing_page(url: str) -> bool:
    """True if this URL's own path already looks like the destination
    listings page (e.g. /careers/jobs, /jobs) rather than a landing page
    still one hop away from the real listings."""
    path = urllib.parse.urlparse(url).path
    return bool(_LISTING_ONLY_HINTS.match(path))


def find_job_listing_page(company_website: str) -> ListingResult:
    """Resolve a company's job LISTINGS page.

    Preference order: the company's OWN domain listings page wins if one
    can be found there (Strategies B/C, following hops from the homepage
    toward the real listings). A known ATS platform link (Greenhouse,
    Lever, Ashby, Workday, etc.) is used only as a FALLBACK — when the
    own-domain hop chain runs dry on a page that doesn't itself already
    look like a final listings page (e.g. a landing page that embeds a
    third-party ATS board rather than hosting listings directly).

    Raises
    ------
    FetchError
        If the company website itself could not be reached.
    CareerPageNotFoundError
        If no plausible listings page could be located by any strategy.
    """
    homepage_links, homepage_url = _fetch_and_extract(company_website)

    # --- Strategy B: heuristic careers-link scan on the homepage --------
    current_url = _find_career_link_heuristic(homepage_links)
    method = "heuristic"

    # --- Strategy C: LLM fallback ----------------------------------------
    if not current_url:
        current_url = _find_career_link_llm(homepage_links, homepage_url)
        method = "llm"

    if not current_url:
        # Nothing at all found on the company's own domain — fall back to
        # an ATS link on the homepage, if one exists.
        ats_hit = _find_ats_link(homepage_links)
        if ats_hit:
            return ListingResult(ats_hit, method="ats")
        raise CareerPageNotFoundError(
            f"Could not locate a job listings page from {company_website}"
        )

    # The link we found might itself just be a landing/marketing page that
    # further links to the real listings — either the company's OWN
    # secondary subdomain/sub-path, or (only as a fallback) a third-party
    # ATS. Keep following the strongest next hop until nothing better
    # appears, up to a small hop limit so we can't loop forever.
    visited = {current_url}
    for _ in range(_MAX_HOPS):
        try:
            page_links, current_url = _fetch_and_extract(current_url)
        except FetchError:
            # Couldn't go further, but we still have a plausible URL from
            # the previous hop — return it rather than failing outright.
            break

        next_url = _find_career_link_heuristic(page_links)
        if next_url and next_url not in visited and next_url != current_url:
            # Still making progress toward a more specific own-domain
            # page — keep going before considering any ATS fallback.
            visited.add(next_url)
            current_url = next_url
            continue

        # No further own-domain candidate found on this page. If this
        # page doesn't already look like the final listings page itself,
        # it may just be a landing page that embeds a third-party ATS
        # board rather than hosting listings directly — check for that
        # ONLY as a fallback, since the own-domain page (once we're
        # already on the real listings) should win.
        if not _looks_like_final_listing_page(current_url):
            ats_hit = _find_ats_link(page_links)
            if ats_hit:
                return ListingResult(ats_hit, method="ats")
        break

    return ListingResult(current_url, method=method)


# ---------------------------------------------------------------------------
# Fetching / parsing
# ---------------------------------------------------------------------------


def _fetch(url: str) -> tuple[str, str]:
    """Fetch a URL, returning (html, final_url_after_redirects)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FetchError(f"Failed to fetch {url}: {exc}") from exc
    return resp.text, resp.url


# Below this link count, we suspect the page's real navigation is rendered
# client-side via JavaScript and a plain HTTP fetch missed it — worth
# retrying with a headless browser that actually executes the page's JS.
_MIN_PLAUSIBLE_LINKS = 8


def _fetch_with_playwright(url: str) -> Optional[tuple[str, str]]:
    """Fetch a URL using a headless browser so client-rendered (JS) content
    is included. Returns None (rather than raising) on any failure, so
    callers can just fall back to whatever the plain fetch already got."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "`playwright` not installed — cannot render JS-heavy pages. "
            "`pip install playwright && playwright install chromium`."
        )
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=_HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                # "networkidle" is unreliable on heavy sites with continuous
                # background activity (analytics, chat widgets, telemetry) —
                # the network may never go fully quiet even after the page
                # has visually finished rendering. domcontentloaded + a short
                # fixed settle time is more robust for this use case.
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                html = page.content()
                final_url = page.url
                return html, final_url
            finally:
                browser.close()
    except Exception:
        logger.exception("Playwright render failed for %s", url)
        return None


def _fetch_and_extract(url: str) -> tuple[list[tuple[str, str]], str]:
    """Fetch a URL and return (links, final_url), automatically retrying
    with a headless browser if either:
      (a) the plain HTTP fetch fails outright (e.g. 403 from bot-detection
          — a real browser fingerprint is far less likely to be blocked), or
      (b) it succeeds but found suspiciously few links (a strong signal the
          page's real nav is JS-rendered).
    """
    try:
        html, final_url = _fetch(url)
        links = _extract_links(html, final_url)
        logger.info("Fetched %s (plain HTTP) — found %d link(s).", final_url, len(links))
    except FetchError as exc:
        logger.warning(
            "Plain HTTP fetch failed for %s (%s) — retrying with a headless "
            "browser, since bot-detection often blocks simple HTTP clients "
            "but not real browser fingerprints.", url, exc,
        )
        rendered = _fetch_with_playwright(url)
        if rendered is None:
            raise  # both plain fetch and Playwright failed — nothing more to try
        rendered_html, rendered_final_url = rendered
        rendered_links = _extract_links(rendered_html, rendered_final_url)
        logger.info(
            "Fetched %s (headless browser, after plain HTTP failure) — found %d link(s).",
            rendered_final_url, len(rendered_links),
        )
        return rendered_links, rendered_final_url

    if len(links) >= _MIN_PLAUSIBLE_LINKS:
        return links, final_url

    logger.warning(
        "Only %d link(s) found on %s via plain HTTP fetch — this often means "
        "the page's real navigation is rendered client-side via JavaScript. "
        "Retrying with a headless browser.", len(links), final_url,
    )
    rendered = _fetch_with_playwright(url)
    if rendered is None:
        return links, final_url  # fall back to whatever we already have

    rendered_html, rendered_final_url = rendered
    rendered_links = _extract_links(rendered_html, rendered_final_url)
    logger.info(
        "Fetched %s (headless browser) — found %d link(s) (vs %d via plain HTTP).",
        rendered_final_url, len(rendered_links), len(links),
    )

    if len(rendered_links) > len(links):
        return rendered_links, rendered_final_url
    return links, final_url


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return [(absolute_href, link_text), ...] for every <a> tag with an href."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        text = a.get_text(strip=True)
        links.append((absolute, text))
    return links


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


# ---------------------------------------------------------------------------
# Strategy A: ATS detection
# ---------------------------------------------------------------------------


def _find_ats_link(links: list[tuple[str, str]]) -> Optional[str]:
    for href, _text in links:
        domain = _domain(href)
        if any(domain == d or domain.endswith("." + d) for d in _ATS_DOMAINS):
            return _normalize_ats_url(href)
    return None


# ---------------------------------------------------------------------------
# Strategy B: heuristics
# ---------------------------------------------------------------------------


def _find_career_link_heuristic(links: list[tuple[str, str]]) -> Optional[str]:
    scored: list[tuple[int, str, str]] = []
    for href, text in links:
        if _is_external_job_board(href):
            continue  # e.g. a "See our openings on LinkedIn" link
        parsed = urllib.parse.urlparse(href)
        path = parsed.path
        domain = parsed.netloc.lower()
        combined = f"{path} {text}"
        score = 0
        if _CAREER_SUBDOMAIN_HINT.search(domain):
            score += 6  # e.g. careers.company.com or apply.careers.company.com
        if _NON_CAREER_SUBDOMAIN_HINT.match(domain):
            score -= 2  # e.g. news.company.com — unlikely to be the real hub
        if _LISTING_ONLY_HINTS.match(path):
            score += 4
        if _JOBS_SUBPAGE_HINT.search(text):
            score += 8  # strongest signal: an explicit "search/view jobs" CTA,
            # distinguishing a landing page's link to the REAL listings from
            # the landing page itself (e.g. "Search Jobs" -> /careers/jobs)
        if _STRONG_HIRE_HINTS.search(text):
            score += 3
        if _STRONG_HIRE_HINTS.search(path):
            score += 2
        if _CAREER_LINK_HINTS.search(text):
            score += 1  # weak signal on its own — a bare "career" mention
        if _CAREER_LINK_HINTS.search(path):
            score += 1
        if _CAREER_FALSE_POSITIVE_HINTS.search(combined):
            score -= 3  # likely a product/marketing page, not hiring
        if score > 0:
            scored.append((score, href, text))
    if not scored:
        logger.info("Heuristic found no career-hint links at all among %d link(s).", len(links))
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    logger.info(
        "Heuristic top candidates: %s",
        [(s, href, text) for s, href, text in scored[:5]],
    )
    return scored[0][1]


# ---------------------------------------------------------------------------
# Strategy C: LLM fallback
# ---------------------------------------------------------------------------
# Uses Groq's chat completion API. Requires GROQ_API_KEY in the environment.
# If it's not set, this fallback is skipped rather than raising, so the
# pipeline still works end-to-end for sites Strategies A/B already handle.

# NOTE: deliberately NOT a reasoning model (e.g. openai/gpt-oss-120b). This
# task is a trivial "pick an index number" classification — a reasoning
# model spends its token budget on hidden chain-of-thought before ever
# emitting the final answer, which with a small max_tokens value meant
# `completion.choices[0].message.content` came back EMPTY every single
# time (confirmed across multiple companies/sites in testing). A fast
# instruct model answers directly with no hidden reasoning phase.
_LLM_MODEL = os.getenv("GROQ_PICKER_MODEL", "openai/gpt-oss-20b")


def _llm_pick_link(links: list[tuple[str, str]], page_url: str, goal: str) -> Optional[str]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        logger.warning("GROQ_API_KEY not set — skipping LLM fallback for %s", page_url)
        return None
    if not links:
        return None

    try:
        from groq import Groq
    except ImportError:
        logger.warning("`groq` package not installed — skipping LLM fallback. `pip install groq`.")
        return None

    # Cap the candidate list so the prompt stays small and cheap.
    candidates = links[:60]
    listing = "\n".join(f"{i}: [{text or '(no text)'}] {href}" for i, (href, text) in enumerate(candidates))

    prompt = f"""You are helping locate a link on a company webpage: {page_url}

Goal: {goal}

Here are the links found on this page (index: [link text] URL):
{listing}

Reply with ONLY the index number of the single best matching link, and nothing else.
If none of the links match, reply with -1."""

    try:
        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=20,
        )
        raw = completion.choices[0].message.content.strip()
        logger.info("LLM raw response for %s: %r", page_url, raw)
    except Exception:
        logger.exception("LLM fallback API call failed for %s", page_url)
        return None

    match = re.search(r"-?\d+", raw)
    if not match:
        logger.warning(
            "LLM response for %s did not contain a parseable index — "
            "treating as no match. Raw response: %r", page_url, raw,
        )
        return None
    index = int(match.group())

    if index < 0 or index >= len(candidates):
        return None
    return candidates[index][0]


def _find_career_link_llm(links: list[tuple[str, str]], homepage_url: str) -> Optional[str]:
    direct_links = [(href, text) for href, text in links if not _is_external_job_board(href)]
    return _llm_pick_link(
        direct_links, homepage_url,
        goal="Find the link that leads to the company's OWN careers/jobs "
             "listings page, where THEY list open positions for people to "
             "work AT this company. Be careful: some companies (e.g. "
             "ed-tech, coaching, career-services businesses) have marketing "
             "pages with 'career' in the name (like 'Career Tracks' or "
             "'Career Programs') that are about their PRODUCT for "
             "customers, not about hiring. Do not pick those — only pick a "
             "page about working at the company itself. Never pick a link "
             "to an external job board or aggregator like LinkedIn, "
             "Indeed, Glassdoor, or Naukri.",
    )


# ---------------------------------------------------------------------------
# Manual smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    TEST_WEBSITE = "https://www.wendys.com/"

    print(f"\nResolving job listings page for:\n  {TEST_WEBSITE}\n")
    try:
        result = find_job_listing_page(TEST_WEBSITE)
        print("Success!")
        print(f"   Listings page : {result.listing_url}")
        print(f"   Resolved via  : {result.method}")
        sys.exit(0)
    except CareerPageError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)