# One stage. The build is a `uv sync` over a lockfile, so a multi-stage image
# would save a layer of build tools and cost the reproducibility of running
# exactly what the lockfile pins.
FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --extra service --no-dev

COPY prompts/ prompts/
COPY fixtures/ fixtures/
COPY web/ web/
RUN mkdir -p /app/data

ENV PATH="/app/.venv/bin:${PATH}"
EXPOSE 8000
CMD ["uvicorn", "aimai_workflows.contract.api:app", "--host", "0.0.0.0", "--port", "8000"]
