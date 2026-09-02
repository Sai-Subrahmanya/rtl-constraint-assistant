# Dockerfile for reproducible RCA + Yosys + (OpenROAD/OpenSTA when built)
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        yosys \
        build-essential cmake ninja-build clang tcl-dev swig \
        git bison flex \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
COPY examples/ examples/
COPY tests/ tests/
COPY configs/ configs/
COPY Makefile ./

RUN pip3 install --no-cache-dir --break-system-packages -e .

# Build OpenSTA from source (optional — large build)
# RUN git clone --depth 1 https://github.com/The-OpenROAD-Project/OpenSTA.git /opt/OpenSTA \
#     && mkdir /opt/OpenSTA/build && cd /opt/OpenSTA/build \
#     && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc) \
#     && ln -s /opt/OpenSTA/build/app/sta /usr/local/bin/sta

WORKDIR /work
EXPOSE 8765
ENTRYPOINT ["rca"]
CMD ["--help"]
