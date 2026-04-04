# ECS 트랜스코딩 파이프라인 마이그레이션 가이드

> PerfectSwing 비디오 처리 파이프라인을 ECS Fargate 기반으로 전환하기 위한 종합 문서
>
> 작성일: 2026-03-19

---

## 1. 배경 및 문제점

### 현재 파이프라인
```
디바이스 (720p 압축) → R2 업로드 → AI 서버 (GPU에서 리사이즈 + 분석)
```

### 문제점
1. **낮은 스트리밍 화질**: 유저가 클라우드 영상을 볼 때 720p만 제공
2. **GPU 비용 낭비**: AI 서버에서 리사이즈(224x224)를 GPU로 수행. 런타임 비용이 나가는 GPU에서 CPU로 충분한 작업을 처리

---

## 2. 솔루션 아키텍처

### 전체 흐름
```
디바이스 (원본) → R2 (원본 저장)
                    ↓
              Supabase Edge Function → ECS Fargate 트리거
                    ↓
              Docker 컨테이너 (CPU, FFmpeg) — 단일 태스크
                ├── 1. AI 인풋 생성 (수초)
                ├── 2. AI 인풋 R2 업로드
                ├── 3. ⚡ 중간 콜백 → Edge Function → AI 서버 즉시 시작
                ├── 4. HLS adaptive streaming 생성 (수분) ← AI와 병렬 실행
                ├── 5. 내보내기용 mp4 생성 (수분, 2-pass VBR)
                └── 6. 최종 콜백 (HLS/export 완료 알림)
```

**핵심 설계**:
- AI 인풋은 수초 만에 완료 → 즉시 AI 서버 트리거
- HLS/export 생성과 AI 분석이 **병렬 실행**
- 단일 ECS 태스크 (원본 1회 다운로드, Fargate 비용 1개)

### ECS 트리거 시점
- `complete-multipart-upload` 액션 완료 시 자동 fire-and-forget
- 기존 `start-analysis` 호출과 독립적

### AI 서버 호출 방식
- ECS에서 AI 인풋 업로드 직후, Docker 내부에서 curl로 Edge Function 콜백
- Edge Function이 AI 서버를 트리거

---

## 3. R2 저장 구조

```
tennis-game-video/
├── original-videos/{videoId}.mp4    ← 디바이스에서 올린 원본
├── ai-input/{videoId}.mp4           ← 224x224 (AI 서버용)
├── hls/{videoId}/
│   ├── master.m3u8                  ← 앱에서 로드하는 진입점
│   ├── 2160p/                       ← 원본 ≥ 4K일 때만 생성
│   │   ├── playlist.m3u8
│   │   └── segment_000.ts ...
│   ├── 1080p/                       ← 원본 ≥ 1080p일 때 생성
│   │   ├── playlist.m3u8
│   │   └── segment_000.ts ...
│   ├── 720p/                        ← 항상 생성
│   │   ├── playlist.m3u8
│   │   └── segment_000.ts ...
│   └── 480p/                        ← 항상 생성
│       ├── playlist.m3u8
│       └── segment_000.ts ...
├── export/{videoId}.mp4             ← 비트레이트 낮춘 내보내기용
└── thumbnails/{videoId}.jpg         ← 기존 썸네일
```

### Public URL 패턴
- 원본: `https://perfectswing.app/original-videos/{videoId}.mp4`
- AI 인풋: `https://perfectswing.app/ai-input/{videoId}.mp4`
- HLS: `https://perfectswing.app/hls/{videoId}/master.m3u8`
- 내보내기: `https://perfectswing.app/export/{videoId}.mp4`

---

## 4. 출력물 상세 스펙

### 4-1. AI 인풋

| 항목 | 값 |
|------|-----|
| 해상도 | 224x224 (letterbox 없이 stretch) |
| FPS | 정확히 30fps (29.97x 안 됨) |
| 코덱 | H.264 (libx264) — H.265보다 디코딩 빠름 |
| 리사이즈 알고리즘 | bicubic |
| 색공간 | bt709 (HDR → SDR 변환) |
| 회전 | autorotate 적용 (FFmpeg 기본) |
| 오디오 | 포함 (AAC 64kbps). AI가 오디오도 사용. 원본에 없으면 빈(무음) 트랙 삽입 |
| Preset | fast |
| CRF | 23 |

**FFmpeg 명령어**:
```bash
# 오디오 있는 경우
ffmpeg -i input.mp4 \
    -vf "scale=224:224:flags=bicubic,fps=fps=30,format=yuv420p" \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
    -c:v libx264 -preset fast -crf 23 \
    -c:a aac -b:a 64k -y \
    ai_input.mp4

# 오디오 없는 경우 (빈 오디오 트랙 삽입)
ffmpeg -i input.mp4 \
    -f lavfi -i anullsrc=channel_layout=stereo:sample_rate=44100 \
    -vf "scale=224:224:flags=bicubic,fps=fps=30,format=yuv420p" \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
    -c:v libx264 -preset fast -crf 23 \
    -c:a aac -b:a 64k -shortest -y \
    ai_input.mp4
```

**주의사항**:
- 비디오 회전 메타데이터에 주의: 회전된 영상이 들어오면 AI가 이상한 결과를 뱉음
- HDR(Dolby Vision, Rec.2020, HDR10+, HLG 등) 비디오는 반드시 SDR(bt709)로 변환
- 오디오 트랙 필수: AI가 오디오도 분석에 사용. 원본에 오디오가 없어도 빈 트랙을 넣어야 함

### 4-2. HLS Adaptive Bitrate Streaming

| Tier | 해상도 | 비트레이트 (비디오) | 생성 조건 |
|------|--------|-------------------|----------|
| 4K | 2160p | ~15Mbps | 원본 ≥ 2160p |
| 1080p | 1080p | ~5Mbps | 원본 ≥ 1080p |
| 720p | 720p | ~2.5Mbps | 항상 |
| 480p | 480p | ~1Mbps | 항상 |

- 세그먼트 형식: `.ts`
- 세그먼트 길이: 6초 (기본값)
- 오디오: AAC 128kbps
- `master.m3u8` → 각 tier별 `playlist.m3u8` → segments
- 원본 해상도보다 높은 tier는 생성하지 않음

### 4-3. 내보내기용 mp4

| 항목 | 값 |
|------|-----|
| 해상도 | 원본 유지 |
| 인코딩 방식 | 2-pass VBR |
| 목표 비트레이트 | 5Mbps |
| 최대 비트레이트 | 8Mbps |
| 버퍼 크기 | 10M |
| 코덱 | H.264 (libx264) |
| 오디오 | AAC 128kbps |
| faststart | 활성화 |

**FFmpeg 명령어**:
```bash
# Pass 1: 분석
ffmpeg -i input.mp4 \
    -c:v libx264 -b:v 5M \
    -pass 1 -f null /dev/null

# Pass 2: 인코딩
ffmpeg -i input.mp4 \
    -c:v libx264 -b:v 5M -maxrate 8M -bufsize 10M \
    -pass 2 -c:a aac -b:a 128k \
    -movflags +faststart -y \
    export.mp4
```

**2-pass VBR 선택 이유**: CRF 대비 처리 시간 약 2배이지만, 파일 크기 대비 품질이 최적. 정적 장면에서는 비트레이트를 낮추고, 빠른 움직임(테니스)에서는 비트레이트를 높여 화질 유지.

---

## 5. AWS 인프라

### 계정 정보
- **AWS 계정 ID**: `004205764624`
- **IAM 유저**: `dev-min` (관리용), `ECS-Fargate` (ECS RunTask 전용 — AmazonECS_FullAccess + PassRole)
- **리전**: `ap-northeast-2` (서울)

### ECR (컨테이너 레지스트리)
- **레포지토리**: `perfectswing/transcoder`
- **URI**: `004205764624.dkr.ecr.ap-northeast-2.amazonaws.com/perfectswing/transcoder`
- **태그**: `latest`

### ECS (컨테이너 서비스)
- **클러스터**: `perfectswing`
- **Task Definition**: `perfectswing-transcoder` (revision 1)
- **Launch Type**: Fargate
- **CPU**: 1024 (1 vCPU)
- **Memory**: 4096 MB (4 GB)
- **Ephemeral Storage**: 20 GiB

### IAM
- **Task Execution Role**: `ecsTaskExecutionRole`
  - ARN: `arn:aws:iam::004205764624:role/ecsTaskExecutionRole`
  - Policy: `AmazonECSTaskExecutionRolePolicy`

### 네트워크
- **VPC**: `vpc-0fa60f264016cd1bd` (default)
- **서브넷**:
  - `subnet-0c71b11ddbe07f27f` (ap-northeast-2a) — Public IP 자동 할당
  - `subnet-081b25c538df63400` (ap-northeast-2b) — Public IP 자동 할당
  - `subnet-0f593898e5a6026ef` (ap-northeast-2c)
  - `subnet-052e28a4df7161827` (ap-northeast-2d)
- **보안 그룹**: `sg-0a2ece9859a20eda6` (default)
  - 아웃바운드: 전체 오픈 (0.0.0.0/0)
  - 인바운드: 불필요 (ECS Fargate는 서버가 아님)

### CloudWatch 로그
- **로그 그룹**: `/ecs/perfectswing-transcoder`
- **스트림 패턴**: `transcoder/transcoder/{taskId}`

---

## 6. R2 구성 (Cloudflare)

| 항목 | 값 |
|------|-----|
| Bucket | `tennis-game-video` |
| Public Base (r2.dev) | `https://pub-9829cbda552a470fb0321ae375a65709.r2.dev` |
| Custom Domain | `https://perfectswing.app` |
| S3 Endpoint 패턴 | `https://{ACCOUNT_ID}.r2.cloudflarestorage.com` |

> **시크릿 (ACCOUNT_ID, ACCESS_KEY_ID, SECRET_ACCESS_KEY)**은 Cloudflare 대시보드 > R2 > API Tokens에서 확인.
> Supabase Edge Function에도 `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`로 등록되어 있음.

---

## 7. 프로젝트 파일 구조

### 인프라 파일
```
infra/ecs-transcoder/
├── Dockerfile              ← Ubuntu 22.04 + FFmpeg + AWS CLI
├── entrypoint.sh           ← 트랜스코딩 파이프라인 스크립트
└── task-definition.json    ← ECS Fargate Task Definition
```

> `infra/` 폴더는 Flutter 빌드에 포함되지 않음. `flutter build`는 `lib/`, `assets/`, `pubspec.yaml` 기준으로 패키징.

### Supabase Edge Function (수정됨)
```
supabase/functions/multipart-upload/index.ts
```

**추가된 코드**:
1. **상단 환경 변수** (ECS 관련):
   - `AWS_ECS_ACCESS_KEY_ID`, `AWS_ECS_SECRET_ACCESS_KEY`
   - `ECS_CLUSTER`, `ECS_TASK_DEFINITION`, `ECS_SUBNETS`, `ECS_SECURITY_GROUPS`, `ECS_REGION`

2. **`triggerEcsTranscode(videoId)` 함수**:
   - `aws4fetch`로 ECS RunTask API 서명 호출
   - Fargate 태스크 시작, 환경 변수로 VIDEO_ID + R2 credentials 주입
   - fire-and-forget (응답 대기 안 함)

3. **`complete-multipart-upload` 액션**:
   - 업로드 완료 후 `triggerEcsTranscode(fileBase)` 호출 추가
   - `.then().catch()` 패턴으로 에러가 업로드 응답에 영향 안 줌

### 현재 구현 상태

| 파일 | 상태 | 비고 |
|------|------|------|
| `Dockerfile` | ✅ 완료 | Ubuntu 22.04 + FFmpeg + AWS CLI |
| `entrypoint.sh` | ⚠️ 부분 완료 | AI인풋 + 1080p 스트리밍만. HLS + export로 변경 필요 |
| `task-definition.json` | ✅ 완료 | Fargate 1vCPU, 4GB |
| `multipart-upload/index.ts` | ⚠️ 부분 완료 | ECS 트리거 추가됨. 환경 변수 미설정, 미배포 |

---

## 8. 테스트 결과

### 테스트 1: 짧은 영상
- **영상**: `2c168ef8-8c52-48bd-b9cf-882dce99931e` (720p, 6초, 1.6MB)
- **결과**: ✅ 성공
  - AI 인풋: 243KB
  - 스트리밍(1080p): 4.1MB
- **이슈**: 첫 시도에서 `curl --aws-sigv4`로 R2 업로드 시 400 에러 발생 → `aws s3 cp --endpoint-url` 방식으로 변경 후 해결
- **R2 업로드 확인**: HTTP 200, Content-Type: video/mp4

### 테스트 2: iPhone 실제 촬영 영상
- **영상**: `202510058008rally1` (iPhone 16 Pro, 720p, 29.79fps, 9.69초, 9.5MB)
- **결과**: ✅ 성공
  - AI 인풋: 159KB (224x224, 30fps, bt709)
  - 스트리밍(1080p): 6.0MB
- **비고**: `SOURCE_URL` 환경변수로 비표준 경로(`original-videos/` 외) 영상 테스트

### 학습 사항
- R2는 `curl --aws-sigv4` 서명이 불안정 → `aws s3 cp`가 안정적
- `entrypoint.sh`에서 `SOURCE_URL` 환경변수로 다운로드 URL 오버라이드 가능
- FFmpeg autorotate는 기본 활성화 — 별도 설정 불필요
- ECS Fargate 프로비저닝 시간: 약 15~30초

---

## 9. 배포 순서

### Phase 1: Edge Function 배포 (서버)
**영향 범위**: 없음. 기존 파이프라인에 ECS가 추가로 파일만 생성.

1. Supabase에 ECS 환경 변수 설정
2. `multipart-upload` Edge Function 배포
3. 테스트 업로드로 ECS 태스크 자동 트리거 확인
4. CloudWatch 로그로 성공 확인
5. R2에 `ai-input/`, `hls/`, `export/` 파일 생성 확인

### Phase 2: AI 서버 인풋 경로 변경 (가벼운 수정)
**영향 범위**: AI 서버만. 하위 호환 유지.

- `ai-input/{videoId}.mp4` 있으면 → 다운받아 바로 사용 (GPU 리사이즈 스킵)
- 없으면 → 기존처럼 `original-videos/`에서 가져와 리사이즈

> **참고**: AI 서버 전면 교체는 하지 않음. 224x224 리사이즈는 GPU에서 비용이 극히 미미 (50K 픽셀, 프레임당 마이크로초 단위). AI 추론이 전체 처리 시간의 95%+ 차지.

### Phase 3: 앱 업데이트
**영향 범위**: 앱 전체.

1. 디바이스 압축 제거, 원본 업로드
2. 비디오 플레이어를 HLS 스트리밍으로 전환
   - iOS: AVPlayer (HLS 네이티브 지원)
   - Android: ExoPlayer (HLS 네이티브 지원)
3. 내보내기 시 `export/{videoId}.mp4` 사용
4. `videoUrl` 응답에 HLS URL 포함

### Phase 0 (Phase 1 이전): 기존 영상 배치 변환
- 앱 업데이트 시점에 기존 영상도 HLS/export가 필요
- DB에서 전체 videoId 목록 추출
- ECS 태스크를 순차/병렬로 실행하는 배치 스크립트 작성

---

## 10. Supabase Edge Function 환경 변수

### 배포 시 설정 필요 (신규)
```bash
supabase secrets set \
  AWS_ECS_ACCESS_KEY_ID="<IAM Access Key>" \
  AWS_ECS_SECRET_ACCESS_KEY="<IAM Secret Key>" \
  ECS_CLUSTER="perfectswing" \
  ECS_TASK_DEFINITION="perfectswing-transcoder" \
  ECS_SUBNETS="subnet-0c71b11ddbe07f27f,subnet-081b25c538df63400" \
  ECS_SECURITY_GROUPS="sg-0a2ece9859a20eda6" \
  ECS_REGION="ap-northeast-2"
```

### 기존 환경 변수 (이미 설정됨)
- `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_PUBLIC_BASE`
- `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
- `ANALYSIS_API_URL`, `ANALYSIS_API_KEY`, `GCP_ANALYZE_URL`
- `DISCORD_WEBHOOK_URL`
- `FCM_SERVICE_ACCOUNT_JSON`, `FCM_PROJECT_ID`
- 기타 (REVENUECAT, AMPLITUDE 등)

---

## 11. CLI 명령어 참조

### Docker 이미지 빌드 & 푸시
```bash
# 빌드 (linux/amd64 필수 — Fargate용)
docker build --platform linux/amd64 \
  -t perfectswing/transcoder:latest \
  infra/ecs-transcoder/

# ECR 로그인
aws ecr get-login-password --region ap-northeast-2 | \
  docker login --username AWS --password-stdin \
  004205764624.dkr.ecr.ap-northeast-2.amazonaws.com

# 태그 & 푸시
docker tag perfectswing/transcoder:latest \
  004205764624.dkr.ecr.ap-northeast-2.amazonaws.com/perfectswing/transcoder:latest

docker push \
  004205764624.dkr.ecr.ap-northeast-2.amazonaws.com/perfectswing/transcoder:latest
```

### ECS 태스크 수동 실행 (테스트)
```bash
aws ecs run-task \
  --cluster perfectswing \
  --task-definition perfectswing-transcoder:1 \
  --launch-type FARGATE \
  --network-configuration '{
    "awsvpcConfiguration": {
      "subnets": ["subnet-0c71b11ddbe07f27f"],
      "securityGroups": ["sg-0a2ece9859a20eda6"],
      "assignPublicIp": "ENABLED"
    }
  }' \
  --overrides '{
    "containerOverrides": [{
      "name": "transcoder",
      "environment": [
        {"name": "VIDEO_ID", "value": "<videoId>"},
        {"name": "R2_ENDPOINT", "value": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"},
        {"name": "R2_ACCESS_KEY", "value": "<R2_ACCESS_KEY>"},
        {"name": "R2_SECRET_KEY", "value": "<R2_SECRET_KEY>"},
        {"name": "R2_BUCKET", "value": "tennis-game-video"},
        {"name": "R2_PUBLIC_BASE", "value": "https://pub-9829cbda552a470fb0321ae375a65709.r2.dev"},
        {"name": "SUPABASE_URL", "value": "<SUPABASE_URL>"},
        {"name": "SUPABASE_ANON_KEY", "value": "<SUPABASE_ANON_KEY>"}
      ]
    }]
  }' \
  --region ap-northeast-2
```

### ECS 태스크 상태 확인
```bash
aws ecs describe-tasks \
  --cluster perfectswing \
  --tasks <taskId> \
  --region ap-northeast-2 \
  --query 'tasks[0].{status:lastStatus,stopCode:stopCode,exitCode:containers[0].exitCode}'
```

### CloudWatch 로그 확인
```bash
aws logs get-log-events \
  --log-group-name /ecs/perfectswing-transcoder \
  --log-stream-name "transcoder/transcoder/<taskId>" \
  --region ap-northeast-2 \
  --query 'events[*].message' \
  --output text
```

### Edge Function 배포
```bash
supabase functions deploy multipart-upload
```

---

## 12. 남은 TODO

### 인프라 (ECS)
- [x] `entrypoint.sh`: HLS multi-bitrate 출력 (480p/720p/1080p/4K)
- [x] `entrypoint.sh`: 내보내기용 mp4 생성 (2-pass VBR)
- [x] HLS 세그먼트 R2 업로드 (`aws s3 sync`)
- [x] Docker 이미지 빌드 & ECR 푸시 (새 계정 `004205764624`)
- [x] ECS 프로덕션 테스트 성공 (exit code 0)
- [ ] `entrypoint.sh`: AI 인풋 완료 후 중간 콜백 활성화 (현재 주석 처리)
- [ ] ECS Task Definition vCPU/메모리 조정 (긴 영상 처리 시)

### 서버 (Supabase)
- [x] Supabase에 ECS 환경 변수 설정
- [x] Edge Function 프로덕션 배포 (ECS 트리거 포함)
- [ ] `ecs-callback` Edge Function 구현 (AI 인풋 완료 → AI 서버 트리거)
- [ ] `ecs-complete` Edge Function 구현 (트랜스코딩 완료 알림)
- [ ] 기존 영상 배치 변환 스크립트 작성

### AI 서버
- [ ] `ai-input/{videoId}.mp4` 우선 사용하도록 인풋 경로 변경

### 앱 (Flutter)
- [x] iOS/Android 원본 업로드로 변경 (디바이스 압축 제거) — `feature/ios-bgupload`
- [ ] HLS 플레이어 전환 (iOS AVPlayer / Android ExoPlayer)
- [ ] 내보내기 시 `export/{videoId}.mp4` 경로 사용
- [ ] `videoUrl` 응답에서 HLS URL 처리

---

## 13. 주요 결정 사항 로그

| 결정 | 선택 | 이유 |
|------|------|------|
| R2 업로드 방식 | `aws s3 cp` | `curl --aws-sigv4`가 R2에서 400 에러 |
| 인프라 위치 | 같은 레포 `infra/` | ECS 작업 1개뿐이라 별도 레포는 과함 |
| ECS 트리거 위치 | 기존 `multipart-upload` Edge Function | 새 함수 대신 기존에 추가가 간단 |
| 트리거 시점 | `complete-multipart-upload` 완료 시 | `start-analysis`보다 앞단에서 자동 실행 |
| 오케스트레이션 | Docker 내 curl 콜백 | EventBridge 등 외부 서비스 불필요 |
| 4K HLS | 포함 | 동남아 등 저속 환경 대응 (adaptive) |
| 내보내기 품질 | 2-pass VBR (5M target, 8M max) | CRF 23보다 품질/용량 최적 |
| AI 서버 교체 | 안 함 | 224x224 리사이즈 비용 극히 미미 (50K 픽셀) |
| AI 인풋 생성 | 유지 | GPU 런타임 절약 (인풋 경로 변경만) |
| AI 호출 타이밍 | AI 인풋 직후 중간 콜백 | ECS 전체 완료 대기 불필요, AI와 HLS 병렬 |
| AI 호출 경로 | ECS → Supabase 콜백 → AI 서버 (간접) | ECS에서 직접 호출하면 AI 서버 변경 시 Docker 리빌드 필요. Supabase 경유하면 환경 변수만 변경 |
| ECS 태스크 분리 | 안 함 (단일 태스크) | 원본 1회 다운로드, Fargate 비용 1개 |
| 시크릿 문서 포함 | 제외 | 보안. 키 이름과 위치만 기록 |

---

## 14. HLS + Export 스펙 (업계 표준 기반)

> 출처: Apple HLS Authoring Spec, Netflix Per-Title Encoding, Mux/Bitmovin 권장사항, Streaming Learning Center

### 14-1. 핵심 문제: AI 인풋과 리소스 분리 필수

```
현재 ECS Fargate: 50 vCPU 할당량, 피크 72% (AI 인풋만으로)
HLS + Export 추가 시: 태스크당 수십분 점유 → vCPU 누적 → 할당량 초과
→ AI 인풋 태스크도 실행 불가 → 서비스 장애
```

**결론: HLS/Export는 AI 인풋과 반드시 다른 리소스 풀에서 처리해야 함.**

| 옵션 | AI 인풋 영향 | 관리 부담 | 비용 | 비고 |
|------|------------|----------|------|------|
| 별도 ECS 클러스터 (CPU) | 없음 (별도 할당량) | 낮음 | 저렴 | Fargate vCPU 할당량은 계정 단위이므로 별도 클러스터로도 분리 안 됨. 별도 계정 필요 |
| AWS Batch + GPU (g4dn) | 없음 (별도 서비스) | 중간 | 중간 | Fargate 할당량과 독립. 콜드스타트 6-8분 |
| MediaConvert | 없음 (별도 서비스) | 없음 | 비쌈 ($15/hr영상) | 서버리스, HLS 네이티브. R2 출력 불가 → S3 경유 필요 |
| 상시 EC2 GPU 인스턴스 | 없음 (별도) | 높음 | 중간 | 콜드스타트 없음. 24시간 과금 |

> ⚠️ Fargate vCPU 할당량은 **계정 + 리전 단위**. 같은 계정에서 클러스터를 나눠도 할당량은 공유됨.
> 완전 분리하려면: 별도 AWS 계정, 또는 Fargate가 아닌 서비스(Batch, MediaConvert, EC2) 사용.

### 14-2. HLS Adaptive Bitrate Ladder

**코덱**: H.264 (High Profile, Level 4.2)
- HEVC: 30-40% 절약이지만 Chrome 데스크톱 미지원 + 라이선스 이슈 → 보류
- AV1: 모바일 하드웨어 디코더 지원 부족 (2027년 이전) → 보류

**세그먼트**: MPEG-TS (.ts), 6초 (Apple 권장)

| Tier | 해상도 | 비트레이트 | maxrate (107%) | bufsize (150%) | 오디오 | 생성 조건 |
|------|--------|-----------|---------------|---------------|--------|----------|
| 4K | 3840x2160 | 15,000 kbps | 16,050 kbps | 22,500 kbps | AAC-LC 192k | 원본 ≥ 2160p |
| 1080p | 1920x1080 | 5,000 kbps | 5,350 kbps | 7,500 kbps | AAC-LC 128k | 원본 ≥ 1080p |
| 720p | 1280x720 | 2,800 kbps | 2,996 kbps | 4,200 kbps | AAC-LC 128k | 항상 |
| 480p | 854x480 | 1,400 kbps | 1,498 kbps | 2,100 kbps | AAC-LC 96k | 항상 |

**비트레이트 근거**:
- 4K 15Mbps: Apple HLS Spec ~16Mbps, Bitmovin 평균 16Mbps
- 1080p 5Mbps: Apple ~6Mbps, Netflix 4.3-5.8Mbps, Mux 스포츠 4-6Mbps
- 720p 2.8Mbps: Apple ~3Mbps, Mux 스포츠 2-3Mbps
- 480p 1.4Mbps: Bitmovin max 2.1Mbps, 저속 네트워크 대응
- maxrate = 비트레이트 × 107%, bufsize = 비트레이트 × 150% (업계 표준 비율)

**필수 FFmpeg 플래그**:
```bash
-c:v libx264 -profile:v high -level 4.2
-g 60 -keyint_min 60 -sc_threshold 0    # 렌디션 간 키프레임 정렬 (ABR 필수)
-preset fast                              # 프로덕션 표준 (medium 대비 2배 빠르고 품질 차이 <3%)
-hls_time 6                               # Apple 권장 세그먼트 길이
-hls_playlist_type vod
-hls_segment_type mpegts                  # H.264 최대 호환성
```

**키프레임 정렬이 필수인 이유**: `-sc_threshold 0`과 동일한 `-g`/`-keyint_min`을 모든 렌디션에 적용하지 않으면, 각 렌디션의 키프레임 위치가 달라져서 ABR 전환 시 끊김 발생.

### 14-3. Export (다운로드/내보내기용)

| 항목 | 값 | 근거 |
|------|-----|------|
| 방식 | Capped CRF | 2-pass VBR와 품질 차이 1.6 VMAF (인지 불가), 인코딩 2배 빠름 |
| CRF | 20 | 스포츠(빠른 움직임)에 적합. CRF 23은 일반 영상용 |
| maxrate | 8 Mbps | 피크 비트레이트 제한 |
| bufsize | 10M | |
| 해상도 | 원본 유지 (다운스케일 안 함) | |
| 코덱 | H.264, High Profile | |
| preset | fast | |
| 오디오 | AAC-LC 128kbps | |
| faststart | 활성화 | 프로그레시브 다운로드 지원 |

**FFmpeg 명령어**:
```bash
ffmpeg -i input.mp4 \
    -c:v libx264 -profile:v high \
    -crf 20 -maxrate 8M -bufsize 10M \
    -preset fast \
    -c:a aac -b:a 128k \
    -movflags +faststart \
    -y export.mp4
```

**Capped CRF vs 2-pass VBR 선택 근거**:
- 품질 차이: VMAF 1.6점 (JND 임계값 3점 미만 → 사람이 구분 못함)
- 인코딩 시간: 1-pass vs 2-pass → 절반
- ECS 비용: 시간 = 돈이므로 1-pass가 유리
- 출처: slhck.info Rate Control Guide, Streaming Learning Center

### 14-4. AI 인풋 비트레이트 결정 (변경 없음)

| 항목 | 값 | 근거 |
|------|-----|------|
| 방식 | CRF 23 | 화질 기준 인코딩, FFmpeg가 비트레이트 자동 결정 |
| 실측 비트레이트 | ~60 kbps | 224x224에서 충분 |

**비트레이트를 올려도 AI 정확도가 안 오르는 이유**: 224x224 리사이즈 시점에서 이미 원본 디테일이 소실됨 (720p 921,600px → 224x224 50,176px = 18배 축소). CRF를 낮춰도 리사이즈에서 잃은 정보는 복구 불가. 비트레이트를 올리면 파일만 커짐.

---

## 15. 관련 파일 빠른 참조

| 파일 | 용도 |
|------|------|
| [`infra/ecs-transcoder/Dockerfile`](infra/ecs-transcoder/Dockerfile) | Docker 이미지 정의 |
| [`infra/ecs-transcoder/entrypoint.sh`](infra/ecs-transcoder/entrypoint.sh) | 트랜스코딩 파이프라인 스크립트 |
| [`infra/ecs-transcoder/task-definition.json`](infra/ecs-transcoder/task-definition.json) | ECS Fargate Task Definition |
| [`supabase/functions/multipart-upload/index.ts`](supabase/functions/multipart-upload/index.ts) | Edge Function (ECS 트리거 포함) |
| [`lib/core/constants/app_constants.dart`](lib/core/constants/app_constants.dart) | R2 URL 상수 |
| [`lib/data/services/upload_service.dart`](lib/data/services/upload_service.dart) | 멀티파트 업로드 |
| [`lib/data/repositories/upload_repository.dart`](lib/data/repositories/upload_repository.dart) | 업로드 플로우 |
| [`lib/data/services/app_video_player.dart`](lib/data/services/app_video_player.dart) | 비디오 플레이어 추상화 |