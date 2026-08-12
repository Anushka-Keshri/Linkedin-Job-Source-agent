# Official Playwright image — Chromium (and its many system dependencies)
# come pre-installed, which avoids the usual headless-browser deployment
# headaches (missing fonts, missing shared libraries, etc.) that plague
# plain python:3.x-slim images when you try to add Playwright yourself.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium is already in this base image, but this confirms Playwright's
# own browser registry is in sync with the installed `playwright` pip
# package version — cheap insurance against a version-mismatch error.
RUN playwright install chromium

COPY . .

# Render sets $PORT at runtime; gunicorn binds to it here.
# One worker is enough for a low-traffic demo/take-home submission —
# increase --workers if you need real concurrency later.
CMD gunicorn -b 0.0.0.0:$PORT --timeout 120 app:app