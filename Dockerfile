# Non-release builds use the maintainer-requested latest published full image.
ARG PLUGIN_DONOR_IMAGE=ghcr.io/ministackorg/ministack:full

# glibc base (not Alpine): DuckDB ships no musl wheel, and the Athena emulator
# needs DuckDB + its httpfs/iceberg extensions to read S3 Tables (Iceberg) tables
# through "s3tablescatalog/<bucket>" the way Athena does on AWS.
FROM python:3.13-slim-bookworm AS builder

RUN pip install --no-cache-dir --no-compile \
        hypercorn==0.18.0 \
        "duckdb>=1.4" \
        "pytz>=2024.1" \
        "cbor2>=5.4.0" \
        "defusedxml>=0.7" \
        "docker>=7.0.0" \
        "pyyaml>=6.0" \
        "cryptography>=41.0" \
        "pymysql>=1.1" \
        "asyncssh>=2.14" \
        "boto3>=1.34" \
        "jsonata-python>=0.7.0" \
        "graphql-core==3.2.12" \
        "awscli==1.45.63"

# Bake the DuckDB extensions the Athena emulator loads at query time into a
# fixed, root-owned directory; the runtime user never downloads anything.
RUN python -c "import duckdb; c = duckdb.connect(); c.execute(\"SET extension_directory = '/opt/duckdb_ext'\"); c.execute('INSTALL httpfs; INSTALL iceberg; LOAD httpfs; LOAD iceberg')" \
    && find /opt/duckdb_ext -type f -name '*.duckdb_extension' | sort

# Strip awscli help examples (~25 MB) and Python cache files (~15 MB).
RUN rm -rf /usr/local/lib/python3.13/site-packages/awscli/examples \
    && find /usr/local/lib/python3.13/site-packages -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip*.dist-info \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip*

# Plugin artifacts are release-stable; release builds pin this donor by digest.
FROM ${PLUGIN_DONOR_IMAGE} AS plugin-donor

FROM python:3.13-slim-bookworm

LABEL maintainer="MiniStack" \
      description="Local AWS Service Emulator — drop-in LocalStack replacement"

# Upgrade base packages to pick up latest security patches.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends nodejs bash openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/* /usr/bin/wget /bin/wget \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* \
              /usr/local/bin/pip*

WORKDIR /opt/ministack

# Copy cleaned Python packages and CLI entrypoints from builder.
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/aws /usr/local/bin/aws
COPY --from=builder /usr/local/bin/aws_completer /usr/local/bin/aws_completer
COPY --from=builder /usr/local/bin/hypercorn /usr/local/bin/hypercorn
COPY --from=builder /opt/duckdb_ext /opt/duckdb_ext

COPY bin/awslocal /usr/local/bin/awslocal
RUN chmod +x /usr/local/bin/awslocal

COPY ministack/ ministack/
COPY --from=plugin-donor /opt/ministack/mysql-plugins /opt/ministack/mysql-plugins

RUN groupadd --system ministack && useradd --system --gid ministack --create-home --home-dir /home/ministack ministack
RUN mkdir -p /tmp/ministack-data/s3 && chown -R ministack:ministack /tmp/ministack-data
RUN mkdir -p /docker-entrypoint-initaws.d/ready.d \
             /etc/localstack/init/boot.d \
             /etc/localstack/init/ready.d && \
    chown -R ministack:ministack /docker-entrypoint-initaws.d /etc/localstack
VOLUME /docker-entrypoint-initaws.d
VOLUME /etc/localstack/init

ARG MINISTACK_VERSION=dev
ENV MINISTACK_VERSION=${MINISTACK_VERSION} \
    GATEWAY_PORT=4566 \
    LOG_LEVEL=INFO \
    S3_PERSIST=0 \
    S3_DATA_DIR=/tmp/ministack-data/s3 \
    REDIS_HOST=redis \
    REDIS_PORT=6379 \
    RDS_BASE_PORT=15432 \
    RDS_PERSIST=0 \
    DSQL_BASE_PORT=25432 \
    DSQL_PERSIST=0 \
    DSQL_STRICT=0 \
    ELASTICACHE_BASE_PORT=16379 \
    LAMBDA_EXECUTOR=local \
    DUCKDB_EXTENSION_DIR=/opt/duckdb_ext \
    USE_SSL=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=2 \
    MALLOC_ARENA_MAX=2

EXPOSE 4566 2222

# Pure Python healthcheck — no curl dependency; USE_SSL for HTTP/HTTPS.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,ssl,urllib.request as r; t=os.environ.get('USE_SSL','').strip().lower() in ('1','true','yes'); r.urlopen(('https' if t else 'http')+'://localhost:4566/_ministack/health',context=ssl._create_unverified_context() if t else None)" || exit 1

ENTRYPOINT ["python", "-m", "ministack"]
