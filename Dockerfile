# Same base image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit (no git in the slim image, so take GitHub's tarball of that commit)
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN python -c "import tarfile; tarfile.open('/tmp/sqlglot.tar.gz').extractall('/tmp/sg')" \
 && mv /tmp/sg/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && rm -rf /tmp/sg /tmp/sqlglot.tar.gz

# Only what the tests we may run in-process import, at the labeled versions.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -c /app/requirements.lock duckdb pandas numpy pytz

COPY selector.py /app/selector.py
COPY br /app/br
COPY tools /app/tools
COPY maps /app/maps

# The run has a read-only disk: compile everything now, readable by "nobody".
RUN python -m compileall -q --invalidation-mode unchecked-hash /opt/sqlglot/sqlglot /opt/sqlglot/tests /app \
 && chmod -R a+rX /opt/sqlglot /app

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 SKIP_INTEGRATION=0 \
    BR_SQLGLOT=/opt/sqlglot BR_MAPS=/app/maps
CMD ["python", "/app/selector.py"]
