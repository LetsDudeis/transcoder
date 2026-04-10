# ===== Stage 1: FFmpeg with NVENC 빌드 =====
FROM nvidia/cuda:12.2.2-devel-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential nasm yasm pkg-config git \
    libx264-dev libmp3lame-dev libfdk-aac-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# nv-codec-headers (NVENC/CUVID API 헤더)
RUN git clone --branch n12.1.14.0 --depth 1 \
    https://git.videolan.org/git/ffmpeg/nv-codec-headers.git /tmp/nv-codec-headers \
    && cd /tmp/nv-codec-headers && make install

# FFmpeg 7.1 소스 빌드 (NVENC + CUDA 필터 포함)
RUN git clone --branch release/6.1 --depth 1 \
    https://git.ffmpeg.org/ffmpeg.git /tmp/ffmpeg \
    && cd /tmp/ffmpeg \
    && ./configure \
        --enable-gpl \
        --enable-nonfree \
        --enable-cuda \
        --enable-cuda-nvcc \
        --enable-cuvid \
        --enable-nvenc \
        --enable-ffnvcodec \
        --enable-libnpp \
        --enable-libx264 \
        --enable-libfdk-aac \
        --enable-libmp3lame \
        --enable-openssl \
        --extra-cflags="-I/usr/local/cuda/include" \
        --extra-ldflags="-L/usr/local/cuda/lib64" \
    && make -j$(nproc) \
    && make install

# ===== Stage 2: 런타임 =====
FROM nvidia/cuda:12.2.2-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    libx264-163 libfdk-aac2 libmp3lame0 \
    libssl3 \
    curl ca-certificates \
    python3 python3-pip \
    && pip3 install --no-cache-dir boto3 google-cloud-storage \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=builder /usr/local/bin/ffprobe /usr/local/bin/ffprobe

COPY entrypoint.py /entrypoint.py

ENTRYPOINT ["python3", "/entrypoint.py"]
