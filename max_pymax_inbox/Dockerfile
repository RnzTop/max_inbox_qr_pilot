FROM python:3.13-slim-bookworm
ARG BUILD_VERSION=1.0.0-rc2
ARG BUILD_ARCH=aarch64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}"
RUN apt-get update && apt-get install -y --no-install-recommends gosu ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir maxapi-python==2.5.0 qrcode==8.2 \
 && useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY *.py /app/
COPY certs/*.pem /app/certs/
COPY run.sh /app/run.sh
CMD ["/bin/sh", "/app/run.sh"]
