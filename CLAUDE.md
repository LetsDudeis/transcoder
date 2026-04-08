# Transcoder Project

## 프로젝트 개요
PerfectSwing 테니스 영상 분석 서비스의 HLS 트랜스코딩 파이프라인.
Cloud Run Job (L4 GPU) + ffmpeg NVENC으로 HLS 변환 → R2 업로드.

## GCP 정보
- 프로젝트 ID: `essential-rig-491206-h9`
- 리전: `us-east4`
- Dev Job: `transcoder-dev` (R2: `tennis-game-video-dev`, 도메인: `dev.perfectswing.app`)
- Prod Job: `transcoder` (R2: `tennis-game-video`, 도메인: `perfectswing.app`)
- GCS 스크래치: `perfectswing-transcoder-scratch`
- Artifact Registry: `us-east4-docker.pkg.dev/essential-rig-491206-h9/transcoder/transcoder`

## 자주 쓰는 명령어

### 프로덕션 영상 HLS 변환
```bash
gcloud run jobs execute transcoder --region us-east4 --update-env-vars VIDEO_ID={VIDEO_ID}
```
결과 HLS: `https://perfectswing.app/hls/{VIDEO_ID}/master.m3u8`

### Dev 영상 HLS 변환
```bash
gcloud run jobs execute transcoder-dev --region us-east4 --update-env-vars VIDEO_ID={VIDEO_ID}
```
결과 HLS: `https://dev.perfectswing.app/hls/{VIDEO_ID}/master.m3u8`

### 로그 확인
```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="{JOB_NAME}" AND labels."run.googleapis.com/execution_name"="{EXECUTION_NAME}"' --project=essential-rig-491206-h9 --limit=30 --format="value(textPayload)"
```

### 이미지 빌드 + 배포
```bash
# 빌드 + push
docker build --platform linux/amd64 -t us-east4-docker.pkg.dev/essential-rig-491206-h9/transcoder/transcoder:{TAG} . && \
docker push us-east4-docker.pkg.dev/essential-rig-491206-h9/transcoder/transcoder:{TAG}

# Job 업데이트
gcloud run jobs update {JOB_NAME} --region us-east4 --image us-east4-docker.pkg.dev/essential-rig-491206-h9/transcoder/transcoder:{TAG}
```

## 아키텍처
- 원본: ffmpeg가 R2 URL 직접 읽기 (다운로드 안 함)
- HLS 세그먼트(.ts): GCS 마운트(`/mnt/hls`)에 쓰기
- playlist/master(.m3u8): 메모리(`/tmp`)에 쓰기 → R2 직접 업로드
- 720p 우선 인코딩 → hls-ready 콜백 → 나머지 티어 1:N 병렬
- 10-bit HEVC 입력 시 scale_cuda format=nv12로 8-bit 변환
- CBR + temporal-aq + preset p2

## R2 접근 정보
- Endpoint: `https://82f39f8a3644c4f2b587970085d3a1bf.r2.cloudflarestorage.com`
- Access Key: `7fb2fed24ca665480e40ad88237d7d25`
- Prod 버킷: `tennis-game-video`
- Dev 버킷: `tennis-game-video-dev`
