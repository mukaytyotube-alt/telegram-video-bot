
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y \
    python3 python3-venv python3-pip git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

RUN git clone --depth 1 --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil

RUN cd /opt/bgutil/server \
    && npm ci \
    && npx tsc

COPY . .

CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js & exec python main.py"]
