FROM --platform=$BUILDPLATFORM golang:1.26.10-bookworm AS uploader
ENV GOTOOLCHAIN=local
ARG YTU_RELEASE=v1.25.5
WORKDIR /src
RUN git clone --depth 1 --branch "${YTU_RELEASE}" https://github.com/porjo/youtubeuploader.git .
COPY build/uploader-modules.txt /tmp/uploader-modules.txt
# Keep upstream application code while replacing vulnerable transitive libraries.
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    xargs go get < /tmp/uploader-modules.txt && go mod tidy && go mod verify && go test ./...
ARG TARGETARCH
ARG TARGETVARIANT
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" GOARM="${TARGETVARIANT#v}" \
    go build -trimpath -ldflags="-s -w -X main.appVersion=${YTU_RELEASE#v}" \
    -o /out/youtubeuploader ./cmd/youtubeuploader
RUN --mount=type=cache,target=/go/pkg/mod \
    mkdir -p /out/licenses && cp /usr/local/go/LICENSE /out/licenses/Go-LICENSE && \
    cp go.mod go.sum /tmp/uploader-modules.txt /out/licenses/ && \
    git rev-parse HEAD > /out/licenses/source-commit && \
    printf '%s\n' 'youtubeuploader dependencies were updated using uploader-modules.txt; see go.mod and go.sum.' > /out/licenses/CHANGES && \
    go list -deps -f '{{with .Module}}{{.Path}} {{.Dir}}{{end}}' ./cmd/youtubeuploader | sort -u | \
    while read -r module directory; do \
        test -n "$directory" || continue; \
        mkdir -p "/out/licenses/$module"; \
        find "$directory" -maxdepth 1 -type f \
            \( -iname 'license*' -o -iname 'notice*' -o -iname 'copying*' \) \
            -exec cp '{}' "/out/licenses/$module/" \;; \
    done

FROM --platform=$BUILDPLATFORM golang:1.26.10-bookworm AS rclone
ENV GOTOOLCHAIN=local
ARG RCLONE_RELEASE=v1.75.2
WORKDIR /src
RUN git clone --depth 1 --branch "${RCLONE_RELEASE}" https://github.com/rclone/rclone.git .
RUN --mount=type=cache,target=/go/pkg/mod go mod download && go mod verify
ARG TARGETARCH
ARG TARGETVARIANT
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" GOARM="${TARGETVARIANT#v}" GOMAXPROCS=2 \
    go build -p 2 -trimpath -ldflags="-s -w -X github.com/rclone/rclone/fs.Version=${RCLONE_RELEASE}" \
    -o /out/rclone .
RUN --mount=type=cache,target=/go/pkg/mod \
    mkdir -p /out/licenses && cp COPYING /out/licenses/Rclone-LICENSE && \
    cp /usr/local/go/LICENSE /out/licenses/Go-LICENSE && cp go.mod go.sum /out/licenses/ && \
    git rev-parse HEAD > /out/licenses/source-commit && \
    go list -deps -f '{{with .Module}}{{.Path}} {{.Dir}}{{end}}' . | sort -u | \
    while read -r module directory; do \
        test -n "$directory" || continue; \
        mkdir -p "/out/licenses/$module"; \
        find "$directory" -maxdepth 1 -type f \
            \( -iname 'license*' -o -iname 'notice*' -o -iname 'copying*' \) \
            -exec cp '{}' "/out/licenses/$module/" \;; \
    done

FROM python:3.12-bookworm AS streamlink
ENV streamlinkCommit=b19cc05aff877e95b4aee2000401566a3ea4d5b5
# Keep the certificate bundle above the critical advisory's fixed release.
RUN pip3 install --no-cache-dir --root-user-action=ignore --prefix=/install certifi==2026.10.19 git+https://github.com/streamlink/streamlink.git@${streamlinkCommit}
RUN pip3 install --no-cache-dir --root-user-action=ignore --prefix=/install websocket-client==1.9.2
RUN sed -i '/arguments.extend(\[/a \                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",' /install/lib/python3.12/site-packages/streamlink/webbrowser/chromium.py

FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.authors="crashahotrod@gmail.com"
LABEL org.opencontainers.image.licenses="MIT"
ARG USER_NAME=apps
ARG USER_ID=568
ARG GROUP_ID=568
ENV USER_NAME=${USER_NAME}
ENV HTTP_STATUS_ENABLED=false
RUN groupadd -r -g ${GROUP_ID} ${USER_NAME} && useradd -r -u ${USER_ID} -g ${USER_NAME} -m -s /bin/bash ${USER_NAME}
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates supervisor jq inotify-tools ffmpeg exiftool chromium chromium-driver gosu libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 dbus-x11 tini util-linux tzdata && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends mesa-va-drivers && \
    if [ "$(dpkg --print-architecture)" = amd64 ]; then \
        apt-get install -y --no-install-recommends intel-media-va-driver i965-va-driver; \
    fi && rm -rf /var/lib/apt/lists/*
COPY --from=uploader /out/youtubeuploader /etc/youtubeuploader
COPY --from=uploader /out/licenses /usr/share/doc/youtubeuploader
COPY --from=rclone /out/rclone /usr/local/bin/rclone
COPY --from=rclone /out/licenses /usr/share/doc/rclone
COPY --from=streamlink /install /usr/local
ENV CHROME_BIN=/usr/bin/chromium CHROME_PATH=/usr/lib/chromium/
RUN dbus-uuidgen > /etc/machine-id
RUN mkdir -p /var/log/streamlink
RUN mkdir -p /config
RUN mkdir -p /storage
RUN mkdir -p /etc/streamlink/tools
RUN mkdir -p /etc/streamlink/scratch
RUN mkdir -p /var/run/dbus

COPY ./download.sh /etc/streamlink/tools/
COPY ./pipeline.sh ./publish.sh ./normalize.sh ./recover.sh ./status.sh ./audit.sh ./media.py /etc/streamlink/tools/
COPY ./notifications.py ./operations.py ./healthcheck.py ./segment_capture.py ./session_recovery.py ./capture.py /etc/streamlink/tools/
COPY ./chat_archive.py ./status_server.py ./dashboard.html /etc/streamlink/tools/
COPY ./content.py ./cloud_sync.py ./publication.py ./validate_content.py ./catchup_vod.sh /etc/streamlink/tools/
COPY ./encode.sh /etc/streamlink/tools/
COPY ./upload.sh /etc/streamlink/tools/
COPY ./supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY pipeline.sh media.py /usr/local/bin/
COPY notifications.py chat_archive.py /usr/local/bin/
COPY content.py cloud_sync.py publication.py validate_content.py /usr/local/bin/
EXPOSE 8080
COPY LICENSE /usr/share/doc/streamlink-recorder/LICENSE
RUN chmod +x /etc/streamlink/tools/*.sh /usr/local/bin/entrypoint.sh && \
    ln -sf /etc/supervisor/conf.d/supervisord.conf /etc/supervisor/supervisord.conf && \
    ln -sf /etc/supervisor/conf.d/supervisord.conf /etc/supervisord.conf

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python3", "/etc/streamlink/tools/healthcheck.py"]

ENTRYPOINT ["/usr/bin/tini", "--","entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]

# BuildKit can execute this offline check under its bundled architecture emulator.
# The default release target below inherits runtime without shipping test fixtures.
FROM runtime AS platform-test
COPY tests/test_platform.sh tests/generate_media_fixtures.py /etc/streamlink/tools/tests/
RUN --network=none MODE=twitch CHANNEL=platform CLIENT_ID=example CLIENT_SECRET=example \
    entrypoint.sh bash /etc/streamlink/tools/tests/test_platform.sh

FROM runtime AS release
