# ── build stage ───────────────────────────────────────────────────────────────
FROM python:3.13-slim AS deps

WORKDIR /build

# SAML SSO is a separate dependency, and an expensive one to build: xmlsec has
# no wheels and needs a compiler and the libxmlsec1 headers. Production installs it
# (the default is unchanged); the dev profile turns it off and gets a noticeably
# faster image. See requirements-saml.txt.
ARG INSTALL_SAML=true

RUN if [ "$INSTALL_SAML" = "true" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            gcc g++ \
            libxmlsec1-dev libxml2-dev pkg-config \
        && apt-get clean && \
        rm -rf /var/lib/apt/lists/* ; \
    fi

COPY requirements.txt requirements-saml.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_SAML" = "true" ]; then \
        pip install --no-cache-dir -r requirements-saml.txt ; \
    fi


# ── runtime stage ─────────────────────────────────────────────────────────────
# ── vendored browser-engine assets ───────────────────────────────────────────
# static/pyodide, static/duckdb-wasm and friends are not in git and are kept out
# of the build context by .dockerignore. deploy/make-bundle.sh passes them as
# the named build context `engine-assets`, which replaces this empty stage; a
# plain `docker build .` gets an image without them (Pyodide then comes from
# the CDN).
FROM scratch AS engine-assets

FROM python:3.13-slim AS runtime

WORKDIR /app

ARG INSTALL_SAML=true

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libxml2 \
        libjemalloc2 \
    && if [ "$INSTALL_SAML" = "true" ]; then \
        apt-get install -y --no-install-recommends libxmlsec1 ; \
    fi \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY --from=deps /usr/local/lib/python3.13/site-packages \
                 /usr/local/lib/python3.13/site-packages
COPY --from=deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# ── application code ──────────────────────────────────────────────────────────
COPY app/          app/
COPY templates/    templates/
COPY static/       static/
COPY --from=engine-assets / static/
# Operator tools (intent_eval, semantics_gate, lake_probe): run inside the
# container with the app's DB and LLM settings — `docker exec … python tools/…`.
COPY tools/        tools/
COPY entrypoint.py entrypoint.py

# ── runtime environment (non-sensitive defaults only) ─────────────────────────
ENV OP_HOST=0.0.0.0 \
    OP_PORT=8743 \
    OP_DB_PATH=/data/onepage.db \
    OP_UPLOADS_DIR=/data/uploads \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LD_PRELOAD=libjemalloc.so.2
# jemalloc: less heap fragmentation for the long-lived render LRU (hundreds of
# MB of large buffers cycling through malloc). Soname resolves per-arch; drop
# the LD_PRELOAD line to fall back to glibc malloc.

RUN mkdir -p /data/uploads

EXPOSE 8743

# deep=1 also reads SQLite and writes into the uploads dir, so an instance with
# a corrupt database or an unmounted volume reports unhealthy instead of ok.
# urlopen raises on the 503 the deep probe returns, which fails the check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8743/health?deep=1')" \
    || exit 1

CMD ["python3", "entrypoint.py"]
