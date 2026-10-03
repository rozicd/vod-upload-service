FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first, separately from app code, for better layer caching.
# README.md is included here (not just src/) because the uv_build backend reads
# it as the package's project.readme when building/installing the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

# 8000: HTTP API, 9464: Prometheus scrape endpoint (OTel metrics)
EXPOSE 8000 9464

CMD ["uvicorn", "upload_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
