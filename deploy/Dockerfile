# The gateway image.
#
# Built by `docker-compose.yml` at the repo root, with the **repo root as the
# build context** — this file lives in `deploy/` but every path below is relative
# to the root, because the image needs `pyproject.toml`, `keel/`, and `config/`.
#
# `python:3.12-slim` rather than alpine: LiteLLM pulls in tokenizers and other
# packages with compiled wheels, and musl builds either fall back to a source
# compile or fail outright. A slim glibc base installs prebuilt wheels and is
# both smaller and faster in practice than an alpine image that has to build.
#
# **Size is dominated by one dependency and it is worth knowing.** `litellm`
# alone is ~104 MB installed and the dependency set totals ~137 MB, so the image
# lands near half a gigabyte. That is the single biggest input to S8's
# five-minute cold start, and it is why the layer ordering below matters.

FROM python:3.12-slim

# Bytecode written at build time rather than on first request, so the container's
# first response is not paying for compilation. Unbuffered stdout so the JSON log
# lines (§6.1) reach `docker compose logs` immediately rather than in 8 KB blocks
# — a buffered demo looks like a hung one.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first, so editing a source file does not reinstall LiteLLM.
# `README.md` is copied with it because `pyproject.toml` declares it as the
# project readme and hatchling reads it during the build.
#
# This helps a *rebuild*. It does nothing for S8, which is a cold start on a
# clean machine where no layer is cached — there the ~137 MB download is the
# floor, and no amount of ordering removes it.
COPY pyproject.toml README.md ./
COPY keel/ ./keel/

# The `bedrock` extra is deliberately not installed: boto3 is not needed until
# Phase 4, and pulling it in here would hide a missing-dependency bug the same
# way CI excluding it does.
RUN pip install .

# Both configs. `config/keel.yaml` is the real one and `deploy/keel.demo.yaml` is
# what compose points KEEL_CONFIG_PATH at — shipping both means switching between
# them is an env-var change rather than a rebuild.
#
# Copied *after* the install because they change more often than dependencies do,
# and neither is part of the wheel: `pyproject.toml` sets
# `packages = ["keel"]`, so config files reach the image only by being copied.
COPY config/ ./config/
COPY deploy/keel.demo.yaml ./deploy/keel.demo.yaml

# Non-root. Nothing here writes to disk — logs go to stdout (§6.1) and all state
# lives in Redis (§5.5) — so the runtime needs no writable path at all.
RUN useradd --create-home --uid 10001 keel && chown -R keel:keel /app
USER keel

EXPOSE 8080

# The module-level `app`, which `keel/api/app.py` documents as existing for
# exactly this. Construction is I/O-free; the config is read in the lifespan, so
# a bad config fails startup rather than import (NFR-4).
#
# One worker on purpose. `MockChaosState` is per-process (ADR 0002), so a second
# worker would receive none of loadgen's chaos calls and half the traffic would
# ignore the demo's error rate. §10 already records single-instance as a known
# limitation.
CMD ["uvicorn", "keel.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
