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
GCS_DIR = Path(f"/mnt/hls/{VIDEO_ID}")        # GCS 마운트
HLS_DIR = GCS_DIR / "hls"                     # 세그먼트(.ts) → GCS
HLS_LOCAL = WORK_DIR / "hls"                  # playlist(.m3u8) → 메모리
INPUT_URL = SOURCE_URL or f"{R2_PUBLIC_BASE}/original-videos/{VIDEO_ID}.mp4"
INPUT_FILE_RAM = WORK_DIR / "original.mp4"    # 원본 → RAM (빠름, 15GB 이하)
MAX_RAM_DOWNLOAD = 15 * 1024 * 1024 * 1024    # 15GB

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
    print(f"ERROR: {msg}")
    supabase_callback({"type": "transcode-failed", "error": msg})
    # 로컬 정리 (GCS HLS는 서빙 중일 수 있으므로 유지, 원본만 삭제)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    if INPUT_FILE_RAM.exists():
        INPUT_FILE_RAM.unlink(missing_ok=True)
    sys.exit(0)


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
            fail(f"ffmpeg error: {stderr.strip()}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.returncode != 0:
            fail(f"Command failed ({cmd[0]}): {result.stderr.strip()}")
        return result


def ffprobe_value(args: list[str]) -> str:
    """ffprobe로 단일 값 추출"""
    cmd = ["ffprobe", "-v", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        print(f"ffprobe failed: cmd={' '.join(cmd[-3:])}, stderr={result.stderr[:200]}", flush=True)
    return result.stdout.strip().strip(",")


def get_s3_client():
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
        config=Config(
            retries={"max_attempts": 10, "mode": "adaptive"},
            connect_timeout=30,
            read_timeout=600,  # 10분 (대용량 파일 전송용)
        ),
    )


def prepare_workdir():
    """작업 디렉토리 생성 + 원본 다운로드"""
    global INPUT_FILE

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    GCS_DIR.mkdir(parents=True, exist_ok=True)
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    HLS_LOCAL.mkdir(parents=True, exist_ok=True)

    # 파일 크기 확인 (S3 API로)
    s3 = get_s3_client()
    file_size = s3.head_object(Bucket=R2_BUCKET, Key=f"original-videos/{VIDEO_ID}.mp4")["ContentLength"]
    print(f"Input: {INPUT_URL} ({file_size / 1024 / 1024:.0f}MB)")

    # 15GB 이하 → RAM(/tmp), 초과 → URL 직접 읽기
    if file_size <= MAX_RAM_DOWNLOAD:
        INPUT_FILE = INPUT_FILE_RAM
        print(f"Downloading to RAM...")
    else:
        INPUT_FILE = INPUT_URL  # ffmpeg가 URL 직접 읽기
        print(f"File too large for RAM ({file_size / 1024 / 1024 / 1024:.1f}GB), using URL streaming")
        return

    t = time.time()
    try:
        from boto3.s3.transfer import TransferConfig
        transfer_config = TransferConfig(
            multipart_chunksize=64 * 1024 * 1024,  # 64MB 청크
            max_concurrency=16,
            use_threads=True,
        )
        s3.download_file(R2_BUCKET, f"original-videos/{VIDEO_ID}.mp4", str(INPUT_FILE), Config=transfer_config)
    except Exception as e:
        fail(f"Download failed: {e}")
    print(f"  [{time.time() - t:.1f}s] Downloaded: {Path(INPUT_FILE).stat().st_size / 1024 / 1024:.0f}MB")


def analyze_video() -> dict:
    """영상 메타데이터 분석 (autorotate 적용 후 실제 해상도 기준)"""
    width = int(ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=width",
        "-of", "csv=p=0", str(INPUT_FILE)
    ]))
    height = int(ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=height",
        "-of", "csv=p=0", str(INPUT_FILE)
    ]))

    # 회전 메타데이터 확인 (tags 또는 side_data)
    rotation = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream_tags=rotate",
        "-of", "csv=p=0", str(INPUT_FILE)
    ])
    if not rotation:
        rotation = ffprobe_value([
            "-select_streams", "v:0", "-show_entries", "stream_side_data=rotation",
            "-of", "csv=p=0", str(INPUT_FILE)
        ])
    rotation = str(int(float(rotation))) if rotation else "0"

    if rotation in ("90", "270"):
        eff_w, eff_h = height, width
    else:
        eff_w, eff_h = width, height

    # FPS 확인 및 정규화 (30 or 60)
    fps_raw = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0", str(INPUT_FILE)
    ])
    num, den = map(int, fps_raw.split("/"))
    original_fps = num / den
    output_fps = 60 if original_fps >= 45 else 30

    # 픽셀 포맷 확인 (10-bit 여부)
    pix_fmt = ffprobe_value([
        "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
        "-of", "csv=p=0", str(INPUT_FILE)
    ])
    is_10bit = "10" in pix_fmt  # yuv420p10le, p010le 등

    # 오디오 트랙 선택
    audio_info = _select_audio_stream()

    is_landscape = eff_w >= eff_h
    long_side = max(eff_w, eff_h)

    print(f"Original: {width}x{height}, rotation: {rotation}, "
          f"effective: {eff_w}x{eff_h}, fps: {original_fps:.2f}->{output_fps}, "
          f"pix_fmt: {pix_fmt}, 10bit: {is_10bit}, audio: {audio_info}")

    has_rotation = rotation in ("90", "270")

    return {
        "eff_w": eff_w,
        "eff_h": eff_h,
        "is_landscape": is_landscape,
        "long_side": long_side,
        "output_fps": output_fps,
        "is_10bit": is_10bit,
        "has_rotation": has_rotation,
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
        "-of", "json", str(INPUT_FILE)
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


def get_scale_filter(tier: int, is_landscape: bool, is_10bit: bool = False, has_rotation: bool = False) -> str:
    """scale_cuda 필터 문자열 생성
    - has_rotation: rotation 메타데이터 있으면 원본 프레임 기준으로 스케일 (회전 전)
    """
    long = TIER_SPECS[tier]["long_side"]
    fmt = ":format=nv12" if is_10bit else ""
    # rotation 있으면 ffmpeg 프레임이 회전 전이라 스케일 방향 반전
    landscape = is_landscape if not has_rotation else not is_landscape
    if landscape:
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

    scale = get_scale_filter(tier, meta["is_landscape"], meta.get("is_10bit", False), meta.get("has_rotation", False))
    spec = TIER_SPECS[tier]

    print(f"Encoding HLS {tier}p...")

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-noautorotate"]

    # GPU 디코드 + GPU 스케일
    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
    cmd += ["-i", str(INPUT_FILE)]

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

    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-noautorotate"]
    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "8"]
    cmd += ["-i", str(INPUT_FILE)]

    if not meta["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    fps = meta["output_fps"]
    gop = fps * HLS_SEGMENT_SEC

    for tier in tiers:
        spec = TIER_SPECS[tier]
        scale = get_scale_filter(tier, meta["is_landscape"], meta.get("is_10bit", False), meta.get("has_rotation", False))
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


def publish_tier(tier: int):
    """playlist를 GCS에 복사 (세그먼트는 이미 GCS에 있음)"""
    playlist_src = HLS_LOCAL / f"{tier}p" / "playlist.m3u8"
    playlist_dst = HLS_DIR / f"{tier}p" / "playlist.m3u8"
    shutil.copy2(str(playlist_src), str(playlist_dst))


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

    remaining_tiers = [t for t in tiers if t != 720]

    HLS_PUBLIC_BASE = os.environ.get("HLS_PUBLIC_BASE", "https://hls.perfectswing.app")

    def publish_master(tier_list):
        generate_master_m3u8(tier_list, meta)
        # master.m3u8을 GCS에 업로드 (Cache-Control: no-cache 설정)
        from google.cloud import storage
        gcs = storage.Client()
        bucket = gcs.bucket("perfectswing-transcoder-scratch")
        blob = bucket.blob(f"{VIDEO_ID}/hls/master.m3u8")
        blob.cache_control = "no-cache"
        blob.upload_from_filename(str(HLS_LOCAL / "master.m3u8"), content_type="application/vnd.apple.mpegurl")

    # 4. 720p 먼저 인코딩 (단독, GPU 독점)
    t = time.time()
    encode_tier(720, meta)
    print(f"  [{time.time() - t:.1f}s] 720p encode")

    # 720p publish + 임시 master.m3u8 (720p만) + hls-ready 콜백
    t = time.time()
    publish_tier(720)
    publish_master([720])
    hls_url = f"{HLS_PUBLIC_BASE}/{VIDEO_ID}/hls/master.m3u8"
    supabase_callback({"type": "hls-ready", "hlsUrl": hls_url})
    print(f"  [{time.time() - t:.1f}s] 720p publish + hls-ready callback")

    # 5. 나머지 티어 1:N 인코딩 (720p 완료 후 시작)
    if remaining_tiers:
        try:
            t = time.time()
            encode_tiers_1n(remaining_tiers, meta)
            print(f"  [{time.time() - t:.1f}s] remaining tiers encode")

            t = time.time()
            for tier in remaining_tiers:
                publish_tier(tier)
                print(f"  [{time.time() - t:.1f}s] {tier}p publish")

            # master.m3u8을 전체 티어로 업데이트
            publish_master(tiers)
            print(f"  [{time.time() - t:.1f}s] master.m3u8 updated with all tiers")
        except SystemExit:
            # 720p는 이미 공유 가능, 나머지만 실패 → failed 보내고 종료
            print("WARNING: remaining tiers failed, but 720p is available")
            supabase_callback({"type": "transcode-failed", "error": "remaining tiers failed (720p available)"})
            shutil.rmtree(WORK_DIR, ignore_errors=True)
            return

    # hls-complete 콜백 (모든 티어 완료)
    supabase_callback({"type": "hls-complete", "hlsUrl": hls_url})
    print(f"  hls-complete callback sent")

    # 6. 정리 (로컬만, GCS의 HLS는 서빙 중이므로 유지)
    shutil.rmtree(WORK_DIR, ignore_errors=True)
    # 원본만 GCS에서 삭제
    if INPUT_FILE != INPUT_URL:
        Path(INPUT_FILE).unlink(missing_ok=True)

    print(f"Transcoding complete for video: {VIDEO_ID} [{time.time() - t_start:.1f}s total]")
    print(f"  HLS: {hls_url}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(str(e))
