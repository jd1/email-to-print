FROM python:3.12-slim
WORKDIR /app
# OS dependencies are declared in requirements.txt (one package per line)
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends $(tr -d '\r' < requirements.txt | grep -v '^[[:space:]]*#' | xargs) \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 poller
COPY poller.py .
ENV PYTHONUNBUFFERED=1 HOME=/home/poller
USER poller
CMD ["python3", "poller.py"]
