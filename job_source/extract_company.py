"""
job_source.extract_company
~~~~~~~~~~~~~~~~~~~~~~~~~~

Given a single LinkedIn job posting URL, extracts the hiring company's name
and their external website URL (e.g. "https://www.acmecorp.com").

Two-step pipeline (see README notes at the bottom of this file for why):
  1. apimaestro/linkedin-job-detail  — fetch the EXACT job by its numeric
     job_id. This returns the correct company_name for that specific
     posting, unlike a keyword/location search scraper which has no
     reliable way to isolate one job.
  2. apify/google-search-scraper     — resolve company_name -> official
     website domain, filtering out LinkedIn/social/aggregator domains
     that commonly appear in search results for a company name.

Typical usage
-------------
    from job_source.extract_company import get_company_info

    info = get_company_info("https://www.linkedin.com/jobs/view/1234567890/")
    print(info.company_name)    # "Acme Corp"
    print(info.company_website) # "https://www.acmecorp.com"
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_JOB_DETAIL_ACTOR_ID: str = os.getenv(
    "APIFY_JOB_DETAIL_ACTOR_ID", "apimaestro/linkedin-job-detail"
)
_SEARCH_ACTOR_ID: str = os.getenv(
    "APIFY_SEARCH_ACTOR_ID", "apify/google-search-scraper"
)

_LINKEDIN_JOB_URL_RE = re.compile(
    r"^https?://(www\.)?linkedin\.com/jobs/(view|search|collections)/",
    re.IGNORECASE,
)

# Domains that show up in "<company> official website" search results but
# are never the company's own site. Anything matching these gets skipped
# when picking the top organic result.
_EXCLUDED_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "wikipedia.org", "indeed.com", "glassdoor.com",
    "crunchbase.com", "bloomberg.com", "google.com", "yelp.com",
    "wellfound.com", "angel.co", "ziprecruiter.com", "monster.com",
    "builtin.com", "pitchbook.com", "owler.com", "tiktok.com",
    # News/media outlets — can outrank a smaller/newer company's own
    # homepage for certain queries (e.g. a high-profile feature article).
    "forbes.com", "techcrunch.com", "businessinsider.com", "reuters.com",
    "nytimes.com", "wsj.com", "cnbc.com", "theverge.com", "axios.com",
    "fastcompany.com", "inc.com", "medium.com", "substack.com",
    # Job aggregators / listing mirrors — republish postings from many
    # companies, so they can rank highly in a "<company> official website"
    # search but are never the company's own site.
    "jobrapido.com", "simplyhired.com", "careerjet.com", "jooble.org",
    "adzuna.com", "neuvoo.com", "trovit.com", "jobisjob.com",
    "talent.com", "jobs2careers.com", "recruit.net", "getwork.com",
    "snagajob.com", "flexjobs.com", "remote.co", "himalayas.app",
}


@dataclass(frozen=True)
class CompanyInfo:
    """Structured result returned by :func:`get_company_info`."""

    company_name: str
    """The hiring company's name as reported by LinkedIn."""

    company_website: str
    """The company's external website URL (never a linkedin.com URL)."""


class JobSourceError(Exception):
    """Base class for all job-source extraction errors."""


class InvalidLinkedInURLError(JobSourceError):
    """Raised when *linkedin_job_url* is not a recognised LinkedIn job URL."""


class ApifyAPIError(JobSourceError):
    """Raised when an Apify actor run fails or returns an unexpected status."""


class CompanyWebsiteMissingError(JobSourceError):
    """Raised when no external company website could be resolved."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_company_info(linkedin_job_url: str) -> CompanyInfo:
    """Return the company name and external website for a LinkedIn job posting.

    Raises
    ------
    InvalidLinkedInURLError
        If *linkedin_job_url* does not look like a LinkedIn jobs URL, or no
        numeric job ID could be extracted from it.
    ApifyAPIError
        If either Apify actor run fails, times out, or returns no items.
    CompanyWebsiteMissingError
        If a company name was found but no plausible official website could
        be resolved via search.
    """
    _validate_url(linkedin_job_url)
    job_id = _extract_job_id(linkedin_job_url)
    if not job_id:
        raise InvalidLinkedInURLError(
            f"Could not extract a numeric job ID from: {linkedin_job_url}"
        )

    client = ApifyClient(_require_api_token())

    company_name = _fetch_company_name(client, job_id, linkedin_job_url)
    company_website = _resolve_company_website(client, company_name)

    return CompanyInfo(company_name=company_name, company_website=company_website)


# ---------------------------------------------------------------------------
# Step 1: fetch the exact job by ID
# ---------------------------------------------------------------------------


def _fetch_company_name(client: ApifyClient, job_id: str, source_url: str) -> str:
    logger.info("Fetching job detail for job_id=%s via %s", job_id, _JOB_DETAIL_ACTOR_ID)

    try:
        run = client.actor(_JOB_DETAIL_ACTOR_ID).call(run_input={"job_id": [job_id]})
    except Exception as exc:
        raise ApifyAPIError(
            f"Actor '{_JOB_DETAIL_ACTOR_ID}' failed to start or timed out: {exc}"
        ) from exc

    run_status = str(run.get("status", "UNKNOWN"))
    if run_status != "SUCCEEDED":
        raise ApifyAPIError(
            f"Actor '{_JOB_DETAIL_ACTOR_ID}' finished with status '{run_status}' "
            f"(expected 'SUCCEEDED'). Run ID: {run.get('id', 'N/A')}."
        )

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise ApifyAPIError(f"Actor '{_JOB_DETAIL_ACTOR_ID}' returned no dataset ID.")

    items = list(client.dataset(dataset_id).iterate_items())
    if not items:
        raise ApifyAPIError(
            f"Actor '{_JOB_DETAIL_ACTOR_ID}' returned no data for job_id={job_id} "
            f"(url: {source_url}). The job may have been removed from LinkedIn."
        )

    item = items[0]
    company_name = (
        (item.get("company_info") or {}).get("name")
        or item.get("companyName")
        or ""
    ).strip()

    if not company_name:
        raise ApifyAPIError(
            f"Actor '{_JOB_DETAIL_ACTOR_ID}' returned no company name for "
            f"job_id={job_id} (url: {source_url}). Raw item: {item!r}"
        )

    logger.info("Resolved company name: %r (job_id=%s)", company_name, job_id)
    return company_name


# ---------------------------------------------------------------------------
# Step 2: resolve company name -> official website
# ---------------------------------------------------------------------------


def _domain_matches_company(domain: str, company_name: str) -> bool:
    """True if the domain's registrable name closely resembles the company name.

    This is a positive signal that a URL is the company's OWN site, rather
    than relying solely on an ever-growing blocklist of news/media domains
    (which can never be complete — see _EXCLUDED_DOMAINS). E.g. for
    "Mercor", this matches "mercor.com" but not "forbes.com" even if a
    Forbes article about Mercor ranks above Mercor's own homepage.
    """
    labels = domain.split(".")
    # Use the registrable/second-level label (e.g. "jobrapido" out of
    # "in.jobrapido.com"), NOT labels[0] — labels[0] is a SUBDOMAIN
    # (region prefixes like "in.", "us.", "careers.", etc.), and matching
    # against a short subdomain like "in" produces false positives for
    # almost any company name containing those two letters. Falls back to
    # labels[0] only for a bare two-label domain (e.g. "acme.com").
    root = labels[-2] if len(labels) >= 2 else labels[0]

    normalized_name = re.sub(r"[^a-z0-9]", "", company_name.lower())
    normalized_root = re.sub(r"[^a-z0-9]", "", root.lower())
    if not normalized_name or not normalized_root:
        return False
    # Guard against short/generic roots (e.g. "co", "hr", "in") producing
    # spurious substring matches against almost any company name.
    if len(normalized_root) < 4:
        return normalized_root == normalized_name
    ratio = difflib.SequenceMatcher(None, normalized_name, normalized_root).ratio()
    return ratio > 0.6 or normalized_name in normalized_root or normalized_root in normalized_name


def _resolve_company_website(client: ApifyClient, company_name: str) -> str:
    # Try a couple of query phrasings — a quoted "official website" query
    # sometimes triggers Google's AI Overview panel, which this scraper
    # can fail to parse, yielding zero organic results even though normal
    # results exist below it. A plainer query is less likely to trigger
    # that panel, so we fall back to it if the first attempt comes up empty.
    queries = [f'"{company_name}" official website', company_name]

    last_had_zero_results = False
    fallback_url: Optional[str] = None

    for query in queries:
        organic = _run_search(client, query)
        if organic is None:
            continue  # actor/run-level failure already logged; try next query
        if not organic:
            last_had_zero_results = True
            logger.warning("Query %r returned zero organic results — trying next query if any.", query)
            continue

        found_any_non_excluded = False
        for result in organic:
            url = (result.get("url") or "").strip()
            if not url:
                continue
            domain = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
            if any(domain == d or domain.endswith("." + d) for d in _EXCLUDED_DOMAINS):
                continue
            found_any_non_excluded = True
            if _domain_matches_company(domain, company_name):
                logger.info("Resolved website for %r (name match): %s", company_name, url)
                return url
            if fallback_url is None:
                # Keep the first plausible non-excluded result as a backup,
                # in case no result's domain closely resembles the company
                # name (e.g. company uses a stylized/unrelated domain).
                fallback_url = url

        if not found_any_non_excluded:
            # Got organic results but all were excluded domains — try next query too.
            logger.warning("Query %r returned only social/aggregator/news domains — trying next query if any.", query)

    if fallback_url:
        logger.warning(
            "No domain closely matched company name %r — falling back to first "
            "plausible non-excluded result: %s", company_name, fallback_url,
        )
        return fallback_url

    if last_had_zero_results:
        raise CompanyWebsiteMissingError(
            f"Search returned no organic results at all for {company_name!r} "
            "(the search engine may have served an AI Overview or similar "
            "panel that the scraper could not parse)."
        )
    raise CompanyWebsiteMissingError(
        f"Could not resolve an official website for {company_name!r} — "
        "only social/aggregator/news domains appeared in the search results."
    )


def _run_search(client: ApifyClient, query: str) -> Optional[list]:
    """Run one search query, returning its organicResults list, or None on
    an actor/run-level failure (as opposed to a run that simply found nothing)."""
    logger.info("Searching via %s (query=%r)", _SEARCH_ACTOR_ID, query)

    try:
        run = client.actor(_SEARCH_ACTOR_ID).call(
            run_input={"queries": query, "maxPagesPerQuery": 1, "resultsPerPage": 10}
        )
    except Exception as exc:
        logger.exception("Actor '%s' failed to start or timed out for query %r: %s", _SEARCH_ACTOR_ID, query, exc)
        return None

    run_status = str(run.get("status", "UNKNOWN"))
    if run_status != "SUCCEEDED":
        logger.warning(
            "Actor '%s' finished with status '%s' for query %r (Run ID: %s).",
            _SEARCH_ACTOR_ID, run_status, query, run.get("id", "N/A"),
        )
        return None

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.warning("Actor '%s' returned no dataset ID for query %r.", _SEARCH_ACTOR_ID, query)
        return None

    items = list(client.dataset(dataset_id).iterate_items())
    if not items:
        return []

    organic = items[0].get("organicResults") or items[0].get("results") or []
    logger.info("Query %r returned %d organic result(s).", query, len(organic))
    return organic


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise InvalidLinkedInURLError("linkedin_job_url must be a non-empty string.")
    if not _LINKEDIN_JOB_URL_RE.match(url.strip()):
        raise InvalidLinkedInURLError(
            f"'{url}' does not look like a LinkedIn jobs URL. "
            "Expected a URL starting with https://www.linkedin.com/jobs/view/, "
            "https://www.linkedin.com/jobs/search/, or "
            "https://www.linkedin.com/jobs/collections/?currentJobId=..."
        )


def _extract_job_id(url: str) -> Optional[str]:
    """Extract the numeric LinkedIn job ID from a URL, handling dirty formats."""
    parsed = urllib.parse.urlparse(url.strip())
    path = parsed.path.rstrip("/")

    if "/jobs/view/" in path:
        match = re.search(r"/jobs/view/(\d+)", path)
        if match:
            return match.group(1)

    qs = urllib.parse.parse_qs(parsed.query)
    job_id = qs.get("currentJobId", [None])[0]
    if job_id:
        match = re.search(r"^(\d+)", str(job_id))
        if match:
            return match.group(1)

    return None


def _require_api_token() -> str:
    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        raise EnvironmentError(
            "APIFY_API_TOKEN is not set. "
            "Copy .env.example -> .env and fill in your token, "
            "or set the environment variable directly."
        )
    return token


# ---------------------------------------------------------------------------
# Manual smoke-test entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    TEST_URL = "https://www.linkedin.com/jobs/view/4446826155/"

    print(f"\nFetching company info for:\n  {TEST_URL}\n")

    try:
        result = get_company_info(TEST_URL)
        print("Success!")
        print(f"   Company name    : {result.company_name}")
        print(f"   Company website : {result.company_website}")
        sys.exit(0)
    except InvalidLinkedInURLError as e:
        print(f"Invalid URL: {e}", file=sys.stderr)
        sys.exit(1)
    except ApifyAPIError as e:
        print(f"Apify API error: {e}", file=sys.stderr)
        sys.exit(2)
    except CompanyWebsiteMissingError as e:
        print(f"Company website missing: {e}", file=sys.stderr)
        sys.exit(3)
    except EnvironmentError as e:
        print(f"Environment error: {e}", file=sys.stderr)
        sys.exit(4)

# ---------------------------------------------------------------------------
# Why the previous single-actor approach was wrong
# ---------------------------------------------------------------------------
# curious_coder/linkedin-jobs-scraper is a SEARCH-results scraper: its own
# docs say to copy a LinkedIn *search* URL (with keyword/location filters)
# and pass it in. `currentJobId` in a URL is LinkedIn's own UI-navigation
# state — the actor has no way to use it to isolate one specific job, so it
# just scrapes whatever generic search that URL implies and returns ~100
# unrelated postings. The old code's "find target_job_id in results, else
# fall back to items[0]" logic was masking this: the ID essentially never
# matched, so it silently returned an arbitrary, unrelated company every
# time. apimaestro/linkedin-job-detail fetches by exact job_id instead,
# which is what this use case actually needs.