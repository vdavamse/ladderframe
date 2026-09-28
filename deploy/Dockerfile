# One image per agent: the harness + one agent root + share/.
#   docker build -f deploy/Dockerfile --build-arg AGENT=coder -t ladderframe-coder .
FROM python:3.12-slim

ARG AGENT=coder
ENV LADDERFRAME_ROOT=/srv/agents/${AGENT} \
    LADDERFRAME_SHARE=/srv/share \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/srv/.venv/bin:$PATH

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends bash curl ca-certificates git \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY packages ./packages
RUN uv sync --frozen --no-dev --package ladderframe --extra all

COPY share ./share
COPY agents/${AGENT} ./agents/${AGENT}

# Install system tools at build time (jq, mq, rg, gh, supercronic) so pods start fast;
# `ladderframe boot` at startup is then a quick, idempotent no-op for them.
RUN ladderframe --root "$LADDERFRAME_ROOT" boot

RUN useradd --create-home --uid 10001 agent \
 && mkdir -p "$LADDERFRAME_ROOT/var" "$LADDERFRAME_ROOT/tmp" \
 && chown -R agent:agent "$LADDERFRAME_ROOT/var" "$LADDERFRAME_ROOT/tmp"
USER agent

COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
