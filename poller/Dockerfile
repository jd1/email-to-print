# Pinned by digest (multi-arch manifest list for 3.14-slim-bookworm);
# re-pin with: crane digest python:3.14-slim-bookworm (or check Docker Hub).
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f
WORKDIR /app
# cups-client = `lp` (submits to host cupsd over IPP). Document conversion
# happens in the Gotenberg container, so no LibreOffice or fonts are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends cups-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 poller \
    && mkdir -p /state && chown poller:poller /state
COPY poller.py .
ENV PYTHONUNBUFFERED=1 HOME=/home/poller XDG_STATE_HOME=/state
USER poller
# Port must match HEALTH_PORT (default 2631). No curl in slim images,
# so the check uses stdlib urllib instead.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2631/health', timeout=4).status == 200 else 1)"
CMD ["python3", "poller.py"]
