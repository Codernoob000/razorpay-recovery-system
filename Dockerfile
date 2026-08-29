# ===========================================================================
# Dockerfile - AI Revenue Recovery Platform
# ===========================================================================
FROM python:3.11-slim

# Set environment defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    DATABASE_URL=sqlite:////app/data/recovery.db

WORKDIR /app

# Install dependencies in a separate layer for Docker cache efficiency
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[dev]"

# Copy application source code and configuration
COPY config.yaml ./
COPY recovery_platform/ ./recovery_platform/
COPY tests/ ./tests/

# Create non-root user and data directory with ownership
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Healthcheck hitting existing GET /metrics endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/metrics')" || exit 1

CMD ["uvicorn", "recovery_platform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
