# Local lab image: the scorer's exact base + labeled deps + sqlglot at the pinned commit.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends git patch && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock coverage==7.10.7
RUN git init -q /sqlglot && cd /sqlglot \
 && git remote add origin https://github.com/tobymao/sqlglot.git \
 && git fetch -q --depth 1 origin eb11aec4be8cb2122afe713a4816662c3f0fb2fa \
 && git checkout -q FETCH_HEAD
ENV PYTHONPATH=/sqlglot SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /sqlglot
COPY br_subcov.py /usr/local/lib/python3.13/site-packages/br_subcov.py
RUN echo "import br_subcov" > /usr/local/lib/python3.13/site-packages/br_subcov.pth
