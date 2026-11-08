FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-sandbox fonts-liberation gosu \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir playwright==1.51.0 \
    && useradd --create-home --uid 10001 pilot
WORKDIR /app
COPY *.py ./
COPY certs ./certs
COPY run.sh /app/run.sh
CMD ["/bin/sh", "/app/run.sh"]
