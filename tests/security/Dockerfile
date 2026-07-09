# GHSA-242f-7fxg-f7wm PoC — Unauthenticated Journal Disclosure via the sync API.
#
# Self-contained harness that (1) REPRODUCES the advisory (an unauthenticated remote read dumps the
# journal when read-auth is off — the historical default), then (2) proves the FIX blocks it while
# an admitted, signed client and the local operator still succeed. Runs against the CURRENT repo.
#
#   docker build -f tests/security/Dockerfile -t ghsa242-poc .      # from the repo root
#   docker run --rm ghsa242-poc                                     # exits 0 iff reproduce+fix hold
#
# The container connects to its OWN non-loopback (eth0) address so the daemon sees a REMOTE source,
# exactly as a hostile tailnet/LAN peer would — loopback would be treated as the trusted operator.

FROM python:3.12-slim

LABEL org.opencontainers.image.description="GHSA-242f PoC: hive-mind sync read-auth reproduce + fixed"

WORKDIR /app
COPY . /app/
RUN pip install --no-cache-dir requests

ENV HIVE_HOME=/tmp/victim-hive
ENV HIVE_OWNER_PASSPHRASE=poc-pass
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "tests/security/poc.py"]
