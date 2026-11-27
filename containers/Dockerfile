# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS node
FROM python:3.12-slim-bookworm AS worker
WORKDIR /opt/harness
COPY aegis/__init__.py aegis/contracts.py aegis/role_tools.py ./aegis/
COPY containers/role.py .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
CMD ["python", "/opt/harness/role.py"]

FROM worker AS runtime
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && node --version
COPY containers/runtime.py .
USER 10001:10001
CMD ["python", "/opt/harness/runtime.py", "serve"]

FROM mcr.microsoft.com/playwright/python:v1.64.1-noble AS qa
WORKDIR /opt/harness
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && node --version \
    && pip install --no-cache-dir playwright==1.64.1
COPY aegis/__init__.py aegis/contracts.py aegis/role_tools.py ./aegis/
COPY containers/role.py containers/runtime.py .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
USER 10001:10001
CMD ["python", "/opt/harness/runtime.py", "qa"]
