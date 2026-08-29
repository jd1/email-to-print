FROM python:3.12-slim
WORKDIR /app
# cups-client = `lp` (submits to host cupsd over IPP). Document conversion
# happens in the Gotenberg container, so no LibreOffice or fonts are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends cups-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 poller
COPY poller.py .
ENV PYTHONUNBUFFERED=1 HOME=/home/poller
USER poller
CMD ["python3", "poller.py"]