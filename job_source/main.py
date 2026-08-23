"""
job_source.main
~~~~~~~~~~~~~~~~

End-to-end pipeline for the "LinkedIn Job Source agent" challenge:

    LinkedIn job URL
        -> (1) company name + company website   [extract_company.py]
        -> (2) job listings page URL             [career_page.py]

Usage
-----
    python -m job_source.main "https://www.linkedin.com/jobs/view/<job_id>/"
"""

from __future__ import annotations

import logging
import sys

from job_source.extract_company import (
    ApifyAPIError,
    CompanyWebsiteMissingError,
    InvalidLinkedInURLError,
    get_company_info,
)
from job_source.career_page import (
    CareerPageError,
    CareerPageNotFoundError,
    FetchError,
    find_job_listing_page,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_pipeline(linkedin_job_url: str) -> tuple[str, str, str]:
    """Run the full pipeline. Returns (company_name, company_website, listing_url)."""
    logger.info("STEP 1: extracting company name + website from %s", linkedin_job_url)
    company_info = get_company_info(linkedin_job_url)
    logger.info(
        "STEP 1 done — company_name=%r, company_website=%r",
        company_info.company_name, company_info.company_website,
    )

    logger.info("STEP 2: finding job listings page on %s", company_info.company_website)
    result = find_job_listing_page(company_info.company_website)
    logger.info(
        "STEP 2 done — listing_url=%r (resolved via %s)",
        result.listing_url, result.method,
    )

    return company_info.company_name, company_info.company_website, result.listing_url


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m job_source.main \"<linkedin_job_url>\"", file=sys.stderr)
        sys.exit(4)
    url = sys.argv[1]

    print(f"\nRunning pipeline for:\n  {url}\n")

    try:
        company_name, company_website, listing_url = run_pipeline(url)

        print("\n=== RESULT ===")
        print(f"Company name        : {company_name}")
        print(f"Company website     : {company_website}")
        print(f"Job listings page   : {listing_url}")

        print("\n=== FINAL OUTPUT (input -> output, per assignment example) ===")
        print(f"{url}\n-> {listing_url}")
        sys.exit(0)
    except (InvalidLinkedInURLError, ApifyAPIError, CompanyWebsiteMissingError) as e:
        print(f"\nStep 1 (LinkedIn -> company) failed: {e}", file=sys.stderr)
        sys.exit(1)
    except (FetchError, CareerPageNotFoundError, CareerPageError) as e:
        print(f"\nStep 2 (company site -> job listings) failed: {e}", file=sys.stderr)
        sys.exit(2)
    except EnvironmentError as e:
        print(f"\nEnvironment error: {e}", file=sys.stderr)
        sys.exit(3)