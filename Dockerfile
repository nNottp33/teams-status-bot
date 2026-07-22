FROM python:3.12-slim

# chromium + matching chromedriver (works on amd64 and arm64),
# procps for the pgrep the bot uses, tzdata for Asia/Bangkok.
# ponytail: not alpine — Selenium Manager has no musl build, saves only ~5%
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium chromium-driver procps tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Bangkok \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir selenium

WORKDIR /app
COPY teams-status-bot.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["./entrypoint.sh"]
