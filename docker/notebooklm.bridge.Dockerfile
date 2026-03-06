FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NOTEBOOKLM_HOME=/data/notebooklm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

RUN mkdir -p /data/notebooklm \
    && chown -R appuser:appuser /data /home/appuser

USER appuser

ENTRYPOINT ["notebooklm", "bridge", "whatsapp"]
