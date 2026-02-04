FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git patch build-essential nodejs npm \
    && npm install -g opencode-ai \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agent

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_runtime.py .

ENV WORKSPACE_DIR=/workspace
ENV OPENCODE_CONFIG_DIR=/root/.config/opencode
RUN mkdir -p ${WORKSPACE_DIR}

ARG OPENCODE_CONFIG_HOST=/home/w/.config/opencode
RUN mkdir -p ${OPENCODE_CONFIG_DIR}
COPY opencode.jsonc ${OPENCODE_CONFIG_DIR}/opencode.jsonc

EXPOSE 8000

CMD ["python", "agent_runtime.py"]
