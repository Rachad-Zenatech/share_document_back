FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    TESSERACT_CMD=/usr/bin/tesseract \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8002

WORKDIR /app

# System packages required by OCR, PDF extraction, PostgreSQL drivers,
# Playwright/Crawl4AI headless browser dependencies, and C extensions.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libpq-dev \
    postgresql-client \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-osd \
    poppler-utils \
    gcc \
    curl \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first for better Docker build caching.
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser and its system runtime libraries.
# Store in /ms-playwright with universal permissions for appuser.
RUN playwright install --with-deps chromium \
    && mkdir -p /ms-playwright \
    && chmod -R 777 /ms-playwright

# Copy application source code.
COPY . .

# Create the persistent-data directory and a non-root application user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/data/upload_files \
    && chown -R 10001:10001 /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# Start both FastAPI (background) and MCP server (foreground)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1 & python server.py"]