# Reproducible local RCA runtime.  The default image contains only RCA and its
# Python/runtime dependencies; open-source EDA tools are deliberately optional.
FROM debian:trixie-slim AS rca-base

LABEL org.opencontainers.image.title="RTL Constraint Assistant" \
      org.opencontainers.image.description="RCA runtime; no proprietary EDA tools or PDK collateral included"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
COPY examples/ examples/

# Installs the declared RCA Python dependencies.  It does not fetch/install
# any EDA executable, PDK, Liberty file, or proprietary software at runtime.
RUN pip3 install --no-cache-dir --break-system-packages -e .

WORKDIR /work
EXPOSE 8765
ENTRYPOINT ["rca"]
CMD ["--help"]

# Optional open-source convenience target.  Build explicitly with:
#   docker build --target open-source-yosys -t rca:yosys .
# It still does not include OpenSTA, a Liberty/PDK, or any commercial tool;
# provision those explicitly and use `rca doctor` before a real flow.
FROM rca-base AS open-source-yosys
RUN apt-get update && apt-get install -y --no-install-recommends yosys \
    && rm -rf /var/lib/apt/lists/*

# Keep the default final target free of optional EDA executables.
FROM rca-base AS runtime
