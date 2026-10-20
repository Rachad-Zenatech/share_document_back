FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8002

WORKDIR /app

# All Python dependencies ship prebuilt manylinux wheels, so no compiler or
# system libraries are needed on top of the slim base image.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code.
COPY . .

# Create the persistent-data directory and a non-root application user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/data/upload_files \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live')" || exit 1

# Start both FastAPI (background) and MCP server (foreground)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1 & python server.py"]
