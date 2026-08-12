"""
app.py
~~~~~~
Minimal Flask server that exposes the LinkedIn Job Source pipeline as a
REST API and serves the frontend UI.

Local dev:
    python app.py
    (then open http://localhost:5000)

Production (Render, via gunicorn — see Dockerfile):
    gunicorn -b 0.0.0.0:$PORT app:app
"""

from __future__ import annotations

import logging
from flask import Flask, jsonify, request, send_from_directory
import os

from job_source.main import run_pipeline
from job_source.extract_company import (
    ApifyAPIError,
    CompanyWebsiteMissingError,
    InvalidLinkedInURLError,
)
from job_source.career_page import (
    CareerPageError,
    CareerPageNotFoundError,
    FetchError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = Flask(__name__, static_folder="frontend", template_folder="frontend")


# ── Serve the frontend ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


# ── API endpoint ────────────────────────────────────────────────────────────

@app.route("/api/search", methods=["POST"])
def search():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Please provide a LinkedIn job URL."}), 400

    try:
        company_name, company_website, listing_url = run_pipeline(url)
        return jsonify(
            {
                "company_name": company_name,
                "company_website": company_website,
                "listing_url": listing_url,
            }
        )
    except InvalidLinkedInURLError as e:
        return jsonify({"error": f"Invalid LinkedIn URL: {e}"}), 400
    except (ApifyAPIError, CompanyWebsiteMissingError) as e:
        return jsonify({"error": f"Step 1 failed (LinkedIn → company): {e}"}), 502
    except (FetchError, CareerPageNotFoundError, CareerPageError) as e:
        return jsonify({"error": f"Step 2 failed (company site → job listings): {e}"}), 502
    except EnvironmentError as e:
        return jsonify({"error": f"Environment error: {e}"}), 500
    except Exception as e:
        logging.exception("Unexpected error during pipeline")
        return jsonify({"error": f"Unexpected error: {e}"}), 500


if __name__ == "__main__":
    # Render (and most PaaS platforms) assign a dynamic port via the PORT
    # env var — hardcoding 5000 would fail in production. debug=False here
    # since debug mode is a security risk and not how the app runs under
    # gunicorn anyway (see Dockerfile) — this __main__ block is for local
    # dev convenience only.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
