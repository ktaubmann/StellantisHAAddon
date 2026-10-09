ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

# home-assistant/builder passes the config.yaml version as BUILD_VERSION
ARG BUILD_VERSION=dev
ENV ADDON_VERSION=${BUILD_VERSION}

ENV LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Debian base (not Alpine): Playwright's Chromium needs glibc.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# The full Chromium, not just the headless shell: the Stellantis IdP detects
# the shell and answers with #failedLogin. See app/login.py.
RUN pip3 install --no-cache-dir --require-hashes -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app/ /app/
COPY rootfs/ /

RUN chmod a+x /etc/services.d/loginworker/run /etc/services.d/loginworker/finish
