ARG TARGETARCH
ARG GO_VERSION=1.26.5
# Build the Go binaries on the NATIVE builder arch and cross-compile to the
# target. go2rtc is CGO_ENABLED=0 pure Go, so this needs no emulation at all.
FROM --platform=$BUILDPLATFORM golang:${GO_VERSION}-alpine3.23 AS go2rtc-builder

ARG TARGETARCH
ARG GO_BUILD_PROCS=""

WORKDIR /src/go2rtc
COPY addon/go2rtc/go.mod addon/go2rtc/go.sum ./
RUN go mod download
COPY addon/go2rtc/ ./
RUN set -eu; \
    set --; \
    if [ -n "${GO_BUILD_PROCS}" ]; then \
        export GOMAXPROCS="${GO_BUILD_PROCS}"; \
        set -- "-p=${GO_BUILD_PROCS}"; \
    fi; \
    CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
        go build "$@" -trimpath -ldflags="-s -w" -o /out/go2rtc .; \
    CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
        go build "$@" -trimpath -ldflags="-s -w" -o /out/petlibro-resolve ./cmd/petlibro-resolve

# Home Assistant names its bases amd64/aarch64; Docker's TARGETARCH is
# amd64/arm64. A build-arg cannot differ per platform inside one multi-arch
# build, so map the two with stage aliases instead.
FROM ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.23 AS runtime-amd64
FROM ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.23 AS runtime-arm64

FROM runtime-${TARGETARCH}

ARG TARGETARCH
ARG BUILD_ARCH=${TARGETARCH}
ARG BUILD_VERSION=dev

RUN apk add --no-cache ffmpeg jq \
    && python -m venv /opt/appdaemon-venv \
    && /opt/appdaemon-venv/bin/pip install --no-cache-dir "AppDaemon>=4.5,<5" "cryptography>=45,<47"

COPY --from=go2rtc-builder /out/go2rtc /usr/local/bin/go2rtc
COPY --from=go2rtc-builder /out/petlibro-resolve /usr/local/bin/petlibro-resolve
COPY addon/appdaemon/src/ /opt/petlibro-local/appdaemon/
COPY state-agent/release-public-key.hex /opt/petlibro-local/release-public-key.hex
COPY addon/templates/ /opt/petlibro-local/templates/
COPY addon/render_config.py /usr/local/bin/petlibro-render-config
COPY addon/run.sh /usr/local/bin/petlibro-local-run
COPY addon/rootfs/ /

RUN chmod 0755 \
        /usr/local/bin/go2rtc \
        /usr/local/bin/petlibro-resolve \
        /usr/local/bin/petlibro-render-config \
        /usr/local/bin/petlibro-local-run \
        /etc/services.d/go2rtc/run \
        /etc/services.d/appdaemon/run

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    org.opencontainers.image.title="Petlibro Local backend" \
    org.opencontainers.image.description="Local PLAF203 MQTT control and camera streaming backend" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source="https://github.com/tannerln7/ha-addon-petlibro-local"
