FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PDD_HOSTED_MODE=true \
    PDD_REQUIRE_PASSCODE=true \
    PDD_TRUSTED_INGRESS_PROXY=aca \
    PORT=8000

WORKDIR /app

# Create non-root runtime user
RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid appuser --shell /bin/bash --create-home appuser

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Default build context excludes private evidence; private replay requires explicit opt-in.
COPY api /app/api
COPY agents /app/agents
COPY skills /app/skills
COPY browser_extension /app/browser_extension

RUN mkdir -p /app/reports /app/runtime_artifacts/screenshots && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Single worker on 1 replica preserves in-memory run artifacts, screenshots, and cart-gate tickets
CMD ["uvicorn", "api.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
