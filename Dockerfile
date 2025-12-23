FROM python:3.13.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8000
# uv config
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update && apt-get install -y gosu dos2unix build-essential python3-dev && apt-get clean \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd app

# Install dependencies via uv
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

# Copy application code
COPY ptbrush /app

# Entrypoint setup
ADD docker-entrypoint.sh /docker-entrypoint.sh
RUN dos2unix /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

# Environment variables
# Add virtualenv to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app

WORKDIR /app

VOLUME ["/app/data"]

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["start"]