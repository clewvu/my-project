FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY kalshi_bot ./kalshi_bot
RUN pip install --no-cache-dir -e ".[research]"

# state (loop JSON, recorder SQLite) and the key live on mounted volumes
VOLUME ["/app/state", "/secrets"]
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["kalshi-bot"]
