# syntax=docker/dockerfile:1
# YourContent craft service: four open-source photo/video apps (PhotoCraft, LightCraft, FilmCraft, EffectCraft from
# github.com/storytold, MIT OR Apache-2.0) driven headlessly over MCP, each run inside bubblewrap.
# Build from the repository root:  docker build -f craft/Dockerfile -t media-lab-craft .
# (compose.yaml does this for the `craft` service). The four builds run in parallel; expect 10-30 minutes.
ARG RUST_IMAGE=rust:1.95-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${RUST_IMAGE} AS build-base
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY craft/build-app.sh /usr/local/bin/build-app
RUN chmod 0755 /usr/local/bin/build-app

# Pinned, reviewed commits. Change one only after reviewing the upstream diff (build scripts, Cargo.lock sources,
# new network/process/dlopen code, the MCP tool list); see craft/README.md.
FROM build-base AS photocraft
RUN --mount=type=cache,id=cargo-registry-photocraft,target=/usr/local/cargo/registry build-app photocraft 6f39aba7f2d424948d5dd647ba1bad7c1d00b9b0

FROM build-base AS lightcraft
RUN --mount=type=cache,id=cargo-registry-lightcraft,target=/usr/local/cargo/registry build-app lightcraft 52f64ce0b9bd9313e24e6bd735f9ce1c9d92e6fe

FROM build-base AS filmcraft
RUN --mount=type=cache,id=cargo-registry-filmcraft,target=/usr/local/cargo/registry build-app filmcraft a4536bcfedf588cbef9ea24df149c70bf99a037d

FROM build-base AS effectcraft
RUN --mount=type=cache,id=cargo-registry-effectcraft,target=/usr/local/cargo/registry build-app effectcraft e38a92e792c07f1c57c5dcfcab37d91932b9e2b8

FROM ${PYTHON_IMAGE}
# bubblewrap: every app runs in its own sandbox (no network, read-only system, only the job folder writable).
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --home-dir /var/lib/craft --create-home --shell /usr/sbin/nologin craft
COPY --from=photocraft /out/bin/ /opt/craft/bin/
COPY --from=lightcraft /out/bin/ /opt/craft/bin/
COPY --from=filmcraft /out/bin/ /opt/craft/bin/
COPY --from=effectcraft /out/bin/ /opt/craft/bin/
COPY --from=photocraft /out/licenses/ /usr/share/licenses/
COPY --from=lightcraft /out/licenses/ /usr/share/licenses/
COPY --from=filmcraft /out/licenses/ /usr/share/licenses/
COPY --from=effectcraft /out/licenses/ /usr/share/licenses/
COPY --from=photocraft /out/versions/ /opt/craft/versions/
COPY --from=lightcraft /out/versions/ /opt/craft/versions/
COPY --from=filmcraft /out/versions/ /opt/craft/versions/
COPY --from=effectcraft /out/versions/ /opt/craft/versions/
WORKDIR /app
# The runner needs only YourContent's store, provenance and craft spec (stdlib Python), not the web app.
COPY medialab/__init__.py medialab/types.py medialab/config.py medialab/store.py medialab/provenance.py \
     medialab/licences.json medialab/craft_spec.py medialab/
COPY craft/__init__.py craft/craftd.py craft/recipes.py craft/mcp_client.py craft/craft-mcp craft/
COPY THIRD-PARTY-NOTICES.md NOTICE LICENSE /usr/share/doc/media-lab/
RUN chmod 0755 craft/craft-mcp && mkdir -p /data /output /uploads && chown 1000:1000 /data
ENV MEDIA_LAB_DATA=/data \
    MEDIA_LAB_COMFY_OUTPUT=/output \
    MEDIA_LAB_UPLOADS=/uploads \
    CRAFT_BIN_DIR=/opt/craft/bin \
    CRAFT_VERSIONS_DIR=/opt/craft/versions \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
# No port: the app and the runner share only the data folder (queue in, status and finished files out).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3", "-m", "craft.craftd", "--check"]
CMD ["python3", "-m", "craft.craftd"]
