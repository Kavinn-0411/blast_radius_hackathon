# Blast Radius selector. linux/amd64, run offline as nobody on a read-only disk.
# Same base digest the commits were labeled in (its Python decides test behaviour).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The exact packages of the labeling environment (pytest, duckdb, pandas, ...).
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# sqlglot at the pinned commit, from its own GitHub repository.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
COPY build/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py ${SQLGLOT_SHA} && rm /tmp/fetch_sqlglot.py

# Warm bytecode + pytest's assertion-rewrite cache so runs never recompile
# unchanged modules (the overlay symlinks back to these __pycache__ dirs).
WORKDIR /opt/sqlglot
RUN python -m compileall -q sqlglot tests \
 && SKIP_INTEGRATION=0 python -m pytest --collect-only -q -p no:cacheprovider tests > /dev/null \
 && chmod -R a+rX /opt/sqlglot

COPY app /app
RUN python -c "import json,pickle; pickle.dump(json.load(open('/app/map.json')), open('/app/map.pkl','wb'), protocol=5)" \
 && rm /app/map.json && python -m compileall -q /app && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
WORKDIR /tmp
CMD ["python", "/app/main.py"]
