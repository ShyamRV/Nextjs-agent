FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: Node.js 20, git (for GitHub publish)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ca-certificates \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Install Python dependencies
COPY pyproject.toml .
RUN uv venv --python 3.11 && uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Copy source
COPY . /app/

EXPOSE 8029

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER 10001

CMD ["python", "agent.py"]
