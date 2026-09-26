# buster is EOL and no longer served from deb.debian.org; this stage only
# fetches two arch-independent .deb files, so the distro here is incidental
FROM debian:bookworm-slim AS download

RUN apt update && apt install -y wget
WORKDIR /deb
RUN wget -O /deb/fonts-ubuntu_0.83-2_all.deb           http://mirrors.kernel.org/ubuntu/pool/main/f/fonts-ubuntu/fonts-ubuntu_0.83-2_all.deb && \
    wget -O /deb/ttf-ubuntu-font-family_0.83-2_all.deb http://mirrors.kernel.org/ubuntu/pool/universe/f/fonts-ubuntu/ttf-ubuntu-font-family_0.83-2_all.deb

FROM node:10.24.1-buster-slim AS build
RUN npm install -g gulp@3.9.0
WORKDIR /app
# trailing slash required: the glob matches two files, and the classic
# (non-BuildKit) builder rejects a multi-source COPY without it
COPY ./package*.json /app/
RUN npm install
COPY . /app
RUN gulp release

FROM scratch AS export
COPY --from=build /app/monitorrent-*.zip .

FROM scratch AS mount
COPY . /app

# bullseye LTS ended in June 2026: security.debian.org still serves an index
# for it but the pool is gone, so apt update succeeds and every package 404s.
# bookworm is current and still carries python 3.9, which the pinned
# requirements target.
FROM python:3.9.25-slim-bookworm
MAINTAINER Alexander Puzynia <werwolf.by@gmail.com>

# For docker layers caching it is better to install Playwight first with all dependencies
# playwright 1.31.1 predates bookworm, so `install --with-deps` asks apt for
# names that release dropped - ttf-unifont (now fonts-unifont), xfonts-cyrillic
# (gone) and libfontconfig (superseded by libfontconfig1) - and aborts. Install
# its dependency list ourselves with the current names instead.
COPY --from=download /deb /deb
RUN apt update && apt install -y curl && \
    dpkg -i /deb/fonts-ubuntu_0.83-2_all.deb && \
    dpkg -i /deb/ttf-ubuntu-font-family_0.83-2_all.deb && \
    rm -rf /deb/*.deb && \
    pip install playwright==1.31.1 && \
    apt install -y --no-install-recommends \
        ffmpeg fonts-ipafont-gothic fonts-liberation fonts-noto-color-emoji fonts-tlwg-loma-otf \
        fonts-wqy-zenhei fonts-unifont xfonts-scalable xvfb \
        libatk1.0-0 libcairo-gobject2 libcairo2 libdbus-1-3 libdbus-glib-1-2 libfontconfig1 \
        libfreetype6 libgdk-pixbuf2.0-0 libglib2.0-0 libgtk-3-0 libpango-1.0-0 libpangocairo-1.0-0 \
        libpangoft2-1.0-0 libx11-6 libx11-xcb1 libxcb-shm0 libxcb1 libxcomposite1 libxcursor1 \
        libxdamage1 libxext6 libxfixes3 libxi6 libxrender1 libxt6 libxtst6 && \
    playwright install firefox && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt is changed not often and again for caching let's install it first
COPY ./requirements.txt /var/www/monitorrent/
RUN pip install --no-cache-dir -r /var/www/monitorrent/requirements.txt && \
    pip install --no-cache-dir PySocks

# Copy update application
COPY --from=build /app/dist /var/www/monitorrent

WORKDIR /var/www/monitorrent

EXPOSE 6687

# Healthcheck
# -L is required: / redirects to /login when there is no session, so without
# following it this reports 302 and the container is permanently unhealthy
HEALTHCHECK --interval=1m --timeout=5s --retries=3 --start-period=30s \
  CMD curl -sSL -o /dev/null -w "%{http_code}" http://localhost:6687 | grep -q 200 || exit 1

CMD ["python", "server.py"]
