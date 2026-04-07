#!/usr/bin/env python3
"""Cloud Run Jobs 트랜스코더: 원본 영상 → HLS 멀티비트레이트 + AI 인풋"""

import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

import boto3

# ===== 환경 변수 =====
VIDEO_ID = os.environ["VIDEO_ID"]
R2_ENDPOINT = os.environ["R2_ENDPOINT"]
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY"]
R2_SECRET_KEY = os.environ["R2_SECRET_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
R2_PUBLIC_BASE = os.environ["R2_PUBLIC_BASE"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
IS_DEBUG = os.environ.get("IS_DEBUG", "false").lower() == "true"
SOURCE_URL = os.environ.get("SOURCE_URL")

WORK_DIR = Path(f"/tmp/transcode/{VIDEO_ID}")
HLS_DIR = Path(f"/mnt/hls/{VIDEO_ID}")       # 세그먼트(.ts) → GCS
HLS_LOCAL = WORK_DIR / "hls"                  # playlist(.m3u8) → 메모리
INPUT_URL = SOURCE_URL or f"{R2_PUBLIC_BASE}/original-videos/{VIDEO_ID}.mp4"

# HLS 설정
HLS_SEGMENT_SEC = 4

# HLS 티어 스펙 (비트레이트는 30fps 기준 kbps. 60fps는 encode_tier에서 1.5배 적용)
# codec: h264 = h264_nvenc, hevc = hevc_nvenc
TIER_SPECS = {
    360:  {"long_side": 640,  "bitrate_30": 600,   "level": "auto", "codec": "h264"},
    720:  {"long_side": 1280, "bitrate_30": 2800,  "level": "auto", "codec": "h264"},
    1080: {"long_side": 1920, "bitrate_30": 5000,  "level": "auto", "codec": "h264"},
    2160: {"long_side": 3840, "bitrate_30": 9000,  "level": "auto", "codec": "hevc"},
}

# 티어 생성 조건: 원본 긴변이 이 값 이상이어야 생성
TIER_THRESHOLDS = {360: 0, 720: 0, 1080: 1920, 2160: 3840}


def supabase_callback(payload: dict):
    """Supabase Edge Function 콜백 (fire-and-forget)"""
    payload["isDebug"] = IS_DEBUG
    payload["videoId"] = VIDEO_ID
    data = json.dumps(payload).encode()
    req = Request(
        f"{SUPABASE_URL}/functions/v1/transcode-callback",
        data=data,
        headers={
            "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urlopen(req, timeout=10)
    except Exception as e:
        print(f"WARNING: Supabase callback failed: {e}")


def fail(msg: str):
    """에러 콜백 보내고 종료"""
    print(f"ERROR: {msg}", file=sys.stderr)
    supabase_callback({"type": "transcode-failed", "error": msg})
    sys.exit(1)


def run(cmd: list[str], **kwargs):
    """subprocess.run 래퍼 (ffmpeg는 progress 파이프로 진행률 출력)"""
    is_ffmpeg = cmd[0] == "ffmpeg"
    if is_ffmpeg:
        cmd = [c for c in cmd if c != "-nostdin"]
        proc = subprocess.Popen(
            cmd + ["-progress", "pipe:1", "-nostats"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        last_log = time.time()
        lines = []
        for line in proc.stdout:
            lines.append(line.strip())
            if line.startswith("out_time="):
                now = time.time()
                if now - last_log >= 10:  # 10초마다 진행률 출력
                    print(f"  progress: {line.strip()}", flush=True)
                    last_log = now
        proc.wait()
        if proc.returncode != 0:
            stderr = proc.stderr.read()
            fail(f"Command failed: {' '.join(cmd[:3])}... stderr: {stderr[:500]}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.returncode != 0:
            fail(f"Command failed: {' '.join(cmd[:3])}... stderr: {result.stderr[:500]}")
        return result


def ffprobe_value(args: list[str]) -> str:
    """ffprobe로 단일 값 추출"""
    cmd = ["ffprobe", "-v", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        print(f"ffprobe failed: cmd={' '.join(cmd[-3:])}, stderr={result.stderr[:200]}", flush=True)
    return result.stdout.strip().strip(",")


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def prepare_workdir():
    """작업 디렉토리 생성"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    HLS_LOCAL.mkdir(parents=True, exist_ok=True)
    print(f"Input: {INPUT_URL}")


def analyze_video() -> dict:
    """영상 메타데이터 분석 (autorotate 적용 후 실제 해상도 기준)"""
    width = int(ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=width",
        "-of", "csv=p=0", INPUT_URL
    ]))
    height = int(ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=height",
        "-of", "csv=p=0", INPUT_URL
    ]))

    # 회전 메타데이터 확인
    rotation = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream_tags=rotate",
        "-of", "csv=p=0", INPUT_URL
    ]) or "0"

    if rotation in ("90", "270"):
        eff_w, eff_h = height, width
    else:
        eff_w, eff_h = width, height

    # FPS 확인 및 정규화 (30 or 60)
    fps_raw = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0", INPUT_URL
    ])
    num, den = map(int, fps_raw.split("/"))
    original_fps = num / den
    output_fps = 60 if original_fps >= 45 else 30

    # 픽셀 포맷 확인 (10-bit 여부)
    pix_fmt = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
        "-of", "csv=p=0", INPUT_URL
    ])
    is_10bit = "10" in pix_fmt  # yuv420p10le, p010le 등

    # 오디오 트랙 선택
    audio_info = _select_audio_stream()

    is_landscape = eff_w >= eff_h
    long_side = max(eff_w, eff_h)

    print(f"Original: {width}x{height}, rotation: {rotation}, "
          f"effective: {eff_w}x{eff_h}, fps: {original_fps:.2f}->{output_fps}, "
          f"pix_fmt: {pix_fmt}, 10bit: {is_10bit}, audio: {audio_info}")

    return {
        "eff_w": eff_w,
        "eff_h": eff_h,
        "is_landscape": is_landscape,
        "long_side": long_side,
        "output_fps": output_fps,
        "is_10bit": is_10bit,
        "has_audio": audio_info["has_audio"],
        "audio_stream_index": audio_info["stream_index"],
    }


def _select_audio_stream() -> dict:
    """오디오 스트림 선택. 플레이어와 동일한 우선순위:
    1) default 플래그가 있는 트랙 중 stereo(2ch) 우선
    2) default 플래그가 있는 트랙 중 채널 수가 2에 가장 가까운 것
    3) default 없으면 전체에서 stereo 우선
    4) 최종 fallback: 첫 번째 트랙"""
    raw = ffprobe_value([
        "-show_entries", "stream=index,channels:stream_disposition=default",
        "-select_streams", "a",
        "-of", "json", INPUT_URL
    ])
    if not raw:
        return {"has_audio": False, "stream_index": None}

    try:
        streams = json.loads(raw).get("streams", [])
    except json.JSONDecodeError:
        return {"has_audio": False, "stream_index": None}

    if not streams:
        return {"has_audio": False, "stream_index": None}

    # default 플래그가 있는 트랙 필터링
    defaults = [s for s in streams if s.get("disposition", {}).get("default", 0) == 1]
    candidates = defaults if defaults else streams

    # stereo(2ch)에 가장 가까운 트랙 선택
    best = min(candidates, key=lambda s: abs(s.get("channels", 2) - 2))
    return {"has_audio": True, "stream_index": best["index"]}


def determine_tiers(long_side: int) -> list[int]:
    """원본 해상도 이하의 HLS 티어 결정"""
    tiers = []
    for tier, threshold in sorted(TIER_THRESHOLDS.items()):
        if long_side >= threshold:
            tiers.append(tier)
    return tiers


def get_scale_filter(tier: int, is_landscape: bool, is_10bit: bool = False) -> str:
    """scale_cuda 필터 문자열 생성 (10-bit 입력 + H.264 출력 시 nv12 변환 포함)"""
    long = TIER_SPECS[tier]["long_side"]
    # 10-bit 입력은 항상 8-bit(nv12)로 변환 (HLS 호환성)
    fmt = ":format=nv12" if is_10bit else ""
    if is_landscape:
        return f"scale_cuda=w={long}:h=-2{fmt}"
    else:
        return f"scale_cuda=w=-2:h={long}{fmt}"


def get_resolution_string(tier: int, is_landscape: bool) -> str:
    """master.m3u8용 해상도 문자열"""
    long = TIER_SPECS[tier]["long_side"]
    if is_landscape:
        return f"{long}x{tier}"
    else:
        return f"{tier}x{long}"


def encode_tier(tier: int, meta: dict):
    """단일 HLS 티어 NVENC 인코딩"""
    seg_dir = HLS_DIR / f"{tier}p"        # 세그먼트 → GCS
    playlist_dir = HLS_LOCAL / f"{tier}p"  # playlist → 메모리
    seg_dir.mkdir(parents=True, exist_ok=True)
    playlist_dir.mkdir(parents=True, exist_ok=True)

    scale = get_scale_filter(tier, meta["is_landscape"], meta.get("is_10bit", False))
    spec = TIER_SPECS[tier]

    print(f"Encoding HLS {tier}p...")

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]

    # GPU 디코드 + GPU 스케일
    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
    cmd += ["-i", INPUT_URL]

    # 오디오 없으면 무음 트랙 삽입
    if not meta["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    # 명시적 스트림 선택
    cmd += ["-map", "0:v:0"]
    if meta["has_audio"]:
        cmd += ["-map", f"0:{meta['audio_stream_index']}"]
    else:
        cmd += ["-map", "1:a:0"]

    # FPS 정규화 + GOP (= 세그먼트 길이 x FPS, 키프레임이 세그먼트 경계에 정확히 맞음)
    fps = meta["output_fps"]
    gop = fps * HLS_SEGMENT_SEC

    # 비트레이트 CBR (60fps면 30fps 기준의 1.5배)
    bitrate_kbps = spec["bitrate_30"] if fps == 30 else int(spec["bitrate_30"] * 1.5)
    bitrate = f"{bitrate_kbps}k"
    bufsize = f"{bitrate_kbps * 2}k"

    # 비디오 필터 + 인코더
    cmd += ["-vf", scale]
    cmd += ["-r", str(fps)]

    if spec["codec"] == "hevc":
        cmd += ["-c:v", "hevc_nvenc", "-preset", "p2", "-profile:v", "main"]
        cmd += ["-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p2", "-profile:v", "high"]

    cmd += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize]
    cmd += ["-temporal-aq", "1"]
    cmd += ["-g", str(gop), "-strict_gop", "1", "-bf", "2"]

    # 오디오 인코더
    cmd += ["-c:a", "aac", "-b:a", "128k"]
    if not meta["has_audio"]:
        cmd += ["-shortest"]

    # HLS 출력
    cmd += ["-f", "hls", "-hls_time", str(HLS_SEGMENT_SEC), "-hls_list_size", "0", "-hls_segment_type", "mpegts"]
    cmd += ["-hls_segment_filename", str(seg_dir / "segment_%03d.ts")]
    cmd += ["-y", str(playlist_dir / "playlist.m3u8")]

    print(f"CMD: {' '.join(cmd)}")
    run(cmd)
    print(f"Done: HLS {tier}p")


def encode_tiers_1n(tiers: list[int], meta: dict):
    """여러 HLS 티어를 1:N으로 동시 인코딩 (디코딩 1회)"""
    for tier in tiers:
        (HLS_DIR / f"{tier}p").mkdir(parents=True, exist_ok=True)
        (HLS_LOCAL / f"{tier}p").mkdir(parents=True, exist_ok=True)

    print(f"Encoding HLS 1:N {tiers}...")

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
    cmd += ["-i", INPUT_URL]

    if not meta["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    fps = meta["output_fps"]
    gop = fps * HLS_SEGMENT_SEC

    for tier in tiers:
        spec = TIER_SPECS[tier]
        scale = get_scale_filter(tier, meta["is_landscape"], meta.get("is_10bit", False))
        seg_dir = HLS_DIR / f"{tier}p"
        playlist_dir = HLS_LOCAL / f"{tier}p"

        bitrate_kbps = spec["bitrate_30"] if fps == 30 else int(spec["bitrate_30"] * 1.5)
        bitrate = f"{bitrate_kbps}k"
        bufsize = f"{bitrate_kbps * 2}k"

        # 각 출력에 대한 스트림 매핑 + 필터 + 인코더 + HLS 설정
        cmd += ["-map", "0:v:0"]
        if meta["has_audio"]:
            cmd += ["-map", f"0:{meta['audio_stream_index']}"]
        else:
            cmd += ["-map", "1:a:0"]

        cmd += ["-vf", scale]
        cmd += ["-r", str(fps)]

        if spec["codec"] == "hevc":
            cmd += ["-c:v", "hevc_nvenc", "-preset", "p2", "-profile:v", "main"]
            cmd += ["-tag:v", "hvc1"]
        else:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p2", "-profile:v", "high"]

        cmd += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bufsize]
        cmd += ["-temporal-aq", "1"]
        cmd += ["-g", str(gop), "-strict_gop", "1", "-bf", "2"]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
        if not meta["has_audio"]:
            cmd += ["-shortest"]

        cmd += ["-f", "hls", "-hls_time", str(HLS_SEGMENT_SEC), "-hls_list_size", "0", "-hls_segment_type", "mpegts"]
        cmd += ["-hls_segment_filename", str(seg_dir / "segment_%03d.ts")]
        cmd += ["-y", str(playlist_dir / "playlist.m3u8")]

    print(f"CMD: {' '.join(cmd)}")
    run(cmd)
    print(f"Done: HLS 1:N {tiers}")


def generate_master_m3u8(tiers: list[int], meta: dict):
    """master.m3u8 생성"""
    fps = meta["output_fps"]
    fps_mult = 1.5 if fps == 60 else 1.0
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for tier in tiers:
        spec = TIER_SPECS[tier]
        video_bps = int(spec["bitrate_30"] * fps_mult * 1000)
        bw = video_bps + 128_000
        res = get_resolution_string(tier, meta["is_landscape"])
        # CODECS: 플레이어가 지원 여부를 사전 판별 (HEVC 미지원 기기는 해당 티어 스킵)
        codecs = "hvc1.1.6.L153.B0,mp4a.40.2" if spec["codec"] == "hevc" else "avc1.640028,mp4a.40.2"
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={res},FRAME-RATE={fps:.3f},CODECS="{codecs}"')
        lines.append(f"{tier}p/playlist.m3u8")

    master = HLS_LOCAL / "master.m3u8"
    master.write_text("\n".join(lines) + "\n")
    print("Generated master.m3u8")


def upload_tier(s3, tier: int):
    """단일 HLS 티어를 R2에 병렬 업로드 (세그먼트: GCS, playlist: 로컬)"""
    seg_dir = HLS_DIR / f"{tier}p"
    playlist_dir = HLS_LOCAL / f"{tier}p"
    prefix = f"hls/{VIDEO_ID}/{tier}p"

    files = []
    # 세그먼트(.ts)는 GCS에서
    files += [(f, "video/MP2T") for f in sorted(seg_dir.iterdir()) if f.suffix == ".ts"]
    # playlist(.m3u8)는 로컬에서
    files += [(f, "application/vnd.apple.mpegurl") for f in sorted(playlist_dir.iterdir()) if f.suffix == ".m3u8"]

    def _upload(item):
        f, ct = item
        s3.upload_file(str(f), R2_BUCKET, f"{prefix}/{f.name}", ExtraArgs={"ContentType": ct, "CacheControl": "public, max-age=2592000"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_upload, files))


def check_gpu():
    """GPU 사용 가능 여부 확인"""
    result = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
    if result.returncode != 0:
        fail("GPU not available. nvidia-smi failed.")
    print(result.stdout)


def main():
    t_start = time.time()
    print(f"Starting transcoding for video: {VIDEO_ID}")

    check_gpu()

    # GPU 모니터링 (백그라운드, 10초 간격)
    dmon = subprocess.Popen(
        ["nvidia-smi", "dmon", "-s", "pucvmet", "-d", "10"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )

    def _print_dmon():
        for line in dmon.stdout:
            line = line.strip()
            if line and not line.startswith("#"):
                print(f"  [gpu] {line}", flush=True)

    import threading
    dmon_thread = threading.Thread(target=_print_dmon, daemon=True)
    dmon_thread.start()

    # 1. 작업 디렉토리 준비
    prepare_workdir()

    # 2. 메타데이터 분석
    meta = analyze_video()

    # 3. 티어 결정
    tiers = determine_tiers(meta["long_side"])
    print(f"HLS tiers: {tiers}")

    s3 = get_s3_client()
    remaining_tiers = [t for t in tiers if t != 720]

    def upload_master(tier_list):
        generate_master_m3u8(tier_list, meta)
        s3.upload_file(
            str(HLS_LOCAL / "master.m3u8"),
            R2_BUCKET,
            f"hls/{VIDEO_ID}/master.m3u8",
            ExtraArgs={"ContentType": "application/vnd.apple.mpegurl", "CacheControl": "no-cache"},
        )

    # 4. 720p 인코딩 + 나머지 1:N 인코딩을 병렬 시작
    import threading

    remaining_error = [None]

    def encode_remaining():
        try:
            if remaining_tiers:
                encode_tiers_1n(remaining_tiers, meta)
        except SystemExit:
            remaining_error[0] = "1:N encode failed"

    # 나머지 티어 인코딩을 백그라운드 스레드로 시작
    if remaining_tiers:
        remaining_thread = threading.Thread(target=encode_remaining)
        remaining_thread.start()

    # 720p 인코딩 (메인 스레드, 우선순위)
    t = time.time()
    encode_tier(720, meta)
    print(f"  [{time.time() - t:.1f}s] 720p encode")

    # 720p 업로드 + 임시 master.m3u8 (720p만) + hls-ready 콜백
    t = time.time()
    upload_tier(s3, 720)
    upload_master([720])
    hls_url = f"{R2_PUBLIC_BASE}/hls/{VIDEO_ID}/master.m3u8"
    supabase_callback({"type": "hls-ready", "hlsUrl": hls_url})
    print(f"  [{time.time() - t:.1f}s] 720p upload + master + hls-ready callback")

    # 5. 나머지 티어 완료 대기 + 업로드
    if remaining_tiers:
        remaining_thread.join()
        if remaining_error[0]:
            fail(remaining_error[0])

        t = time.time()
        for tier in remaining_tiers:
            upload_tier(s3, tier)
            print(f"  [{time.time() - t:.1f}s] {tier}p upload")

        # master.m3u8을 전체 티어로 업데이트
        upload_master(tiers)
        print(f"  [{time.time() - t:.1f}s] master.m3u8 updated with all tiers")

    # 6. 정리
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    shutil.rmtree(HLS_DIR, ignore_errors=True)

    print(f"Transcoding complete for video: {VIDEO_ID} [{time.time() - t_start:.1f}s total]")
    print(f"  HLS: {hls_url}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(str(e))
