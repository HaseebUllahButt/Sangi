FROM node:22-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g opencode-ai

COPY package.json package-lock.json ./
RUN npm ci --no-fund --no-audit

COPY . .
RUN mkdir -p creds downloads workspace task-queue outbox uploads brain logs

EXPOSE 4096

CMD ["bash", "start.sh"]