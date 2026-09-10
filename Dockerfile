# syntax=docker/dockerfile:1.7

# STAGE 1: Compile Python bytecode, encrypt it, and build loader.so + host.
FROM python:3.12-slim AS protect-builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        build-essential \
        cmake \
        libssl-dev \
        binutils \
    && rm -rf /var/lib/apt/lists/*

COPY packages/ /packages/
COPY protect/requirements.txt /tmp/protect-requirements.txt
RUN python -m pip install \
        --no-index \
        --find-links=/packages \
        -r /tmp/protect-requirements.txt

COPY . /src/

# The secret is available only to this build instruction. key_data.h is compiled
# into libloader.so and removed with the temporary build directory. The VMP SDK
# is mounted only for this instruction and is never copied into a build layer.
RUN --mount=type=secret,id=source_key,required=true \
    --mount=type=bind,from=vmp_tools,target=/run/vmp-tools,ro \
    python /src/protect/pack.py \
        --root /src \
        --modules-file /src/protect/modules.txt \
        --key-file /run/secrets/source_key \
        --output /out/code.bin \
        --key-header /tmp/lexora-key/key_data.h \
    && cmake \
        -S /src/protect/loader \
        -B /tmp/loader-build \
        -DKEY_HEADER_DIR=/tmp/lexora-key \
        -DVMP_SDK_INCLUDE=/run/vmp-tools/sdk \
        -DVMP_SDK_LIB=/run/vmp-tools/libVMProtectSDK64.so \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/loader-build --target loader_so host --config Release --parallel \
    && install -m 0755 /tmp/loader-build/libloader.so /out/libloader.so \
    && install -m 0755 /tmp/loader-build/host /out/host \
    && rm -rf /tmp/lexora-key /tmp/loader-build


# STAGE 2: VMProtect-pack the sensitive loader shared library. vmprotect_con
# requires glibc >= 2.38, so this stage cannot use python:3.12-slim/bookworm.
FROM ubuntu:24.04 AS vmp-pack

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /tmp/vmp-work

COPY --from=protect-builder /out/libloader.so /tmp/vmp-work/libloader.so

# The writable bind mount is required to normalize the executable bit when the
# context comes from Windows. BuildKit discards writes to this mount after RUN.
RUN --mount=type=bind,from=vmp_tools,target=/run/vmp-tools,rw \
    chmod 0755 /run/vmp-tools/vmprotect_con \
    && mkdir -p /out \
    && cd /run/vmp-tools \
    && LD_LIBRARY_PATH=/run/vmp-tools /run/vmp-tools/vmprotect_con /tmp/vmp-work/libloader.so 2>&1 | tee /tmp/vmp-pack.log \
    && test -s /tmp/vmp-work/libloader.vmp.so \
    && grep -q "lexora_key_reconstruct" /tmp/vmp-pack.log \
    && grep -q "lexora_aes_decrypt" /tmp/vmp-pack.log \
    && grep -q "Compilation completed" /tmp/vmp-pack.log \
    && install -m 0755 /tmp/vmp-work/libloader.vmp.so /out/libloader.vmp.so


# STAGE 3: Final runtime image. VMProtect build tools and the unprotected
# libloader.so are intentionally excluded.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        vim \
        curl \
        dnsutils \
        iputils-ping \
        iproute2 \
        netcat-openbsd \
        procps \
        less \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY packages/ /packages/
RUN python -m pip install \
        --no-index \
        --find-links=/packages \
        -r /app/requirements.txt \
    && rm -rf /packages

# Install the repository-owned tokenizer data without network access.
COPY tools/docxindex/assets/cl100k_base.tiktoken.gz /tmp/cl100k_base.tiktoken.gz
RUN --network=none python -c "import gzip, hashlib, pathlib; source = pathlib.Path('/tmp/cl100k_base.tiktoken.gz'); data = gzip.decompress(source.read_bytes()); assert hashlib.sha256(data).hexdigest() == '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7'; target = pathlib.Path('/opt/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4'); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)" \
    && rm /tmp/cl100k_base.tiktoken.gz \
    && python -c "import tiktoken; assert tiktoken.get_encoding('cl100k_base').name == 'cl100k_base'"

# Protected backend code and host entrypoint.
COPY --from=protect-builder /out/host /app/host
COPY --from=vmp-pack /out/libloader.vmp.so /app/libloader.vmp.so
COPY --from=protect-builder /out/code.bin /app/code.bin

# Runtime resources that are intentionally readable or bind-mounted.
COPY frontend/ /app/frontend/
COPY agents/prompts/cn_prompts.yaml /app/agents/prompts/cn_prompts.yaml
COPY resources/criteria/criteria-routes.yaml /app/resources/criteria/criteria-routes.yaml
COPY tools/document/reporting/pandoc_xelatex_template.tex /app/tools/document/reporting/pandoc_xelatex_template.tex

RUN mkdir -p /app/data /app/profiles /app/logs /app/reports

EXPOSE 8000

CMD ["/app/host", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
