FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Dependencies come from uv.lock (--frozen), so every build installs the versions CI tested.
ENV PYTHONUNBUFFERED=1 CELL_APP_ROOT=/app \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md cell.yaml ./
COPY src ./src
COPY migrations ./migrations
COPY db ./db
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python3 \
    && useradd --system --uid 10001 --no-create-home cell
USER cell
# Secrets (database URLs, public key, publisher token) arrive as environment variables at runtime only.
CMD ["offers-api"]
