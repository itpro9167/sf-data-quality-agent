# syntax=docker/dockerfile:1
#
# sfdq public demo: the review dashboard on fake data, no credentials needed.
#   docker build -t sfdq-demo .
#   docker run --rm -p 8000:8000 sfdq-demo        # then open http://localhost:8000

# Base image is overridable (private registry, Docker Hub mirror, newer Python):
#   docker build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim -t sfdq-demo .
ARG PYTHON_IMAGE=python:3.12-slim

# ---- build stage: install the package and its dependencies into a virtualenv -------------------
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install .

# ---- runtime stage: slim image, non-root user, nothing but the venv ----------------------------
FROM ${PYTHON_IMAGE}
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SFDQ_MODE=mock \
    SFDQ_PUBLIC_DEMO=1 \
    SFDQ_HOME=/data \
    PORT=8000

RUN useradd --create-home --uid 10001 sfdq \
 && mkdir /data && chown sfdq:sfdq /data
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

USER sfdq
WORKDIR /home/sfdq
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"

ENTRYPOINT ["entrypoint.sh"]
