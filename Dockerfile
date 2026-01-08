FROM python:3.12.2
LABEL org.opencontainers.image.authors="crashahotrod@gmail.com"
ARG USER_NAME=apps
ARG USER_ID=568
ARG GROUP_ID=568
ENV USER_NAME=${USER_NAME}
RUN groupadd -g ${GROUP_ID} ${USER_NAME} && useradd -u ${USER_ID} -g ${USER_NAME} -m -s /bin/bash ${USER_NAME}
ARG YTU_RELEASE=v1.25.5
ARG YTU_SHORT="${YTU_RELEASE#v}"
ARG BINARY_DOWNLOAD_URL="https://github.com/porjo/youtubeuploader/releases/download/${YTU_RELEASE}/youtubeuploader_${YTU_SHORT}_Linux_amd64.tar.gz"
RUN apt-get update && apt-get install -y supervisor python3-pip jq inotify-tools ffmpeg exiftool chromium chromium-driver gosu libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 && rm -rf /var/lib/apt/lists/*
RUN curl -L -o youtubeuploader.tar.gz "${BINARY_DOWNLOAD_URL}"
RUN tar -xzf youtubeuploader.tar.gz -C /etc youtubeuploader
ENV streamlinkCommit=68fff72d45465a5792db92defcb3864aed9e052e
ENV CHROME_BIN=/usr/bin/chromium CHROME_PATH=/usr/lib/chromium/
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
RUN chmod +x /etc/streamlink/tools/*.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]