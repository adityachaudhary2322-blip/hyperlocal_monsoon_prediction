# Monsoon AI control panel - Hugging Face Space, Docker SDK, free cpu-basic tier.
#
# Build:  docker build -t monsoon-ai .
# Run:    docker run --rm -p 7860:7860 --env-file .env monsoon-ai
#
# Notes that are specific to a Space rather than to Docker in general:
#
# * A Space runs the container as **user 1000**, and only $HOME and /tmp are writable.
#   Everything that writes at runtime - the Hugging Face cache, Streamlit's config, the
#   Matplotlib font cache - is pointed at $HOME explicitly, because each of those
#   libraries otherwise picks a path that is read-only here and dies on import.
# * There is **no persistent disk**. State lives in Postgres (DATABASE_URL) and model
#   artifacts are fetched from a private model repo (HF_MODEL_REPO) at startup, so a
#   rebuilt or restarted Space loses nothing.
# * Port 7860 is the Space convention and must match `app_port` in README.md.

FROM python:3.11-slim

# libgomp1 is LightGBM's OpenMP runtime - the wheel links against it and import fails
# without it. curl is only for the healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# Matches the uid a Space runs as, so files created at runtime are writable.
RUN useradd --create-home --uid 1000 appuser

ENV HOME=/home/appuser \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # huggingface_hub writes the model snapshot here; the default (~/.cache) is fine
    # only because HOME is set above, so it is named explicitly to be sure.
    HF_HOME=/home/appuser/.cache/huggingface \
    MPLCONFIGDIR=/home/appuser/.config/matplotlib \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

# Dependencies first: this layer is cached while only application code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# To put Chronos-2 in the image, uncomment both lines. Read the file first - it
# explains why this is off by default and what has to be built before it helps.
# COPY requirements-chronos.txt ./
# RUN pip install --no-cache-dir -r requirements-chronos.txt

# Application code, config, and the simplified map layers. .dockerignore keeps the
# research data, the trained models and .env out of the image.
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser scripts/ ./scripts/

# The app writes nothing here, but src/common.py resolves these paths and a download
# of the feature table lands in data/processed/.
RUN mkdir -p /app/data/processed /app/models/lgbm /app/outputs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 7860

# A Space restarts the container on a failed healthcheck rather than serving a blank
# page. Streamlit answers /_stcore/health once the server is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:7860/_stcore/health || exit 1

CMD ["streamlit", "run", "app/main.py", \
     "--server.port", "7860", \
     "--server.address", "0.0.0.0", \
     "--server.headless", "true"]
