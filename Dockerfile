ARG TARGETPLATFORM
ARG TARGETOS
ARG TARGETARCH
ARG ARCH_CODE

FROM python:3.12.2
LABEL org.opencontainers.image.authors="crashahotrod@gmail.com"
ARG YTU_RELEASE=1.25.5
RUN set -e; \
    if [ -z "$TARGETPLATFORM" ]; then \
        echo "Error: TARGETPLATFORM is not set." >&2 && exit 1; \
    fi; \
    OS_LOWER=$(echo "$TARGETPLATFORM" | cut -d/ -f1); \
    ARCH_CODE=$(echo "$TARGETPLATFORM" | cut -d/ -f2); \
    OS_NAME=$(echo "$OS_LOWER" | awk '{print toupper(substr($0, 1, 1)) substr($0, 2)}'); \
    PLATFORM_CODE="${OS_NAME}_${ARCH_CODE}"; \
    echo "Resolved PLATFORM_CODE: $PLATFORM_CODE"; \
    echo "$PLATFORM_CODE" > /tmp/ytu_platform_code
ARG YTU_PLATFORM_CODE=$(cat /tmp/ytu_platform_code)
ARG BINARY_DOWNLOAD_URL="https://github.com/porjo/youtubeuploader/releases/download/v${YTU_RELEASE}/youtubeuploader_${YTU_RELEASE}_${YTU_PLATFORM_CODE}.tar.gz"
RUN echo "Building for Platform:${TARGETPLATFORM} OS/ARCH:${TARGETOS}/${TARGETARCH} Code:${ARCH_CODE}"
RUN echo "Downloading ${BINARY_DOWNLOAD_URL}..." curl -L -o /youtubeuploader.tar.gz "${BINARY_DOWNLOAD_URL}"
RUN tar -xzf /youtubeuploader.tar.gz -C /etc/ youtubeuploader
ENV streamlinkCommit=5a83a3806b5941639c3751ac15a9fed175019b31
RUN apt-get update && apt-get install supervisor python3-pip jq inotify-tools ffmpeg exiftool -y
RUN pip3 install --upgrade git+https://github.com/streamlink/streamlink.git@${streamlinkCommit}
RUN mkdir -p /config
RUN mkdir -p /storage
RUN mkdir -p /etc/streamlink/tools
RUN mkdir -p /etc/streamlink/scratch

COPY ./download.sh /etc/streamlink/tools/
COPY ./encode.sh /etc/streamlink/tools/
COPY ./upload.sh /etc/streamlink/tools/
COPY ./supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /etc/streamlink/tools/download.sh
RUN chmod +x /etc/streamlink/tools/encode.sh
RUN chmod +x /etc/streamlink/tools/upload.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]