# Supabase ↔ HLS Transcoder 연동 가이드

## 전체 아키텍처

```
1. 영상 업로드 완료
2. Supabase Edge Function → Cloud Run Job 트리거 (video_id 전달)
3. 트랜스코더가 HLS 생성 → R2 업로드
4. 완료 시 트랜스코더 → Supabase Edge Function 콜백
```

---

## 1. 트랜스코더 트리거 (Supabase → Cloud Run Job)

### 동시 실행 제한

L4 GPU 할당량: 프로젝트당 리전당 **최대 3개** 동시 실행.
4개 이상 요청 시 GPU 할당 실패 → Job이 대기하거나 실패할 수 있음.
Supabase 측에서 동시 실행 수를 관리하거나, 실패 시 재시도 로직 필요.
(할당량 증가는 GCP에 요청 가능)

### 인증 방식: GCP 서비스 계정 OAuth2

Edge Function에서 서비스 계정 키로 OAuth2 토큰을 생성하여 Cloud Run Jobs API를 호출합니다.

### 환경변수 (Supabase secrets)

```
GCP_SERVICE_ACCOUNT_KEY = <서비스 계정 키 JSON 전체 — 별도 전달>
GCP_PROJECT_ID = "essential-rig-491206-h9"
GCP_REGION = "us-east4"
GCP_JOB_NAME = "transcoder-dev"  (prod: "transcoder")
```

서비스 계정: `transcoder-trigger@essential-rig-491206-h9.iam.gserviceaccount.com`
키 파일은 별도 전달. 절대 코드에 커밋하지 말 것.

dev/prod 분리:
- dev: `GCP_JOB_NAME = "transcoder-dev"`
- prod: `GCP_JOB_NAME = "transcoder"`

### Edge Function 코드 예시 (Deno/TypeScript)

```typescript
// trigger-transcode/index.ts

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

// === GCP OAuth2 토큰 생성 ===
async function getGcpAccessToken(serviceAccountKey: any): Promise<string> {
  const header = { alg: "RS256", typ: "JWT" };
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: serviceAccountKey.client_email,
    scope: "https://www.googleapis.com/auth/cloud-platform",
    aud: "https://oauth2.googleapis.com/token",
    iat: now,
    exp: now + 3600,
  };

  // Base64url 인코딩
  const encode = (obj: any) =>
    btoa(JSON.stringify(obj))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");

  const unsignedToken = `${encode(header)}.${encode(payload)}`;

  // PEM → CryptoKey
  const pemBody = serviceAccountKey.private_key
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\n/g, "");
  const keyData = Uint8Array.from(atob(pemBody), (c) => c.charCodeAt(0));
  const cryptoKey = await crypto.subtle.importKey(
    "pkcs8",
    keyData,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );

  // 서명
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    cryptoKey,
    new TextEncoder().encode(unsignedToken)
  );
  const sig = btoa(String.fromCharCode(...new Uint8Array(signature)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");

  const jwt = `${unsignedToken}.${sig}`;

  // 토큰 교환
  const tokenRes = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`,
  });
  const tokenData = await tokenRes.json();
  return tokenData.access_token;
}

// === Cloud Run Job 실행 ===
async function triggerTranscodeJob(
  videoId: string,
  sourceUrl?: string
): Promise<{ success: boolean; error?: string }> {
  const serviceAccountKey = JSON.parse(
    Deno.env.get("GCP_SERVICE_ACCOUNT_KEY")!
  );
  const projectId = Deno.env.get("GCP_PROJECT_ID")!;
  const region = Deno.env.get("GCP_REGION")!;
  const jobName = Deno.env.get("GCP_JOB_NAME")!;

  const accessToken = await getGcpAccessToken(serviceAccountKey);

  // 환경변수 오버라이드
  const env = [{ name: "VIDEO_ID", value: videoId }];
  if (sourceUrl) {
    env.push({ name: "SOURCE_URL", value: sourceUrl });
  }

  const url = `https://run.googleapis.com/v2/projects/${projectId}/locations/${region}/jobs/${jobName}:run`;

  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      overrides: {
        containerOverrides: [{ env }],
      },
    }),
  });

  if (!res.ok) {
    const err = await res.text();
    return { success: false, error: `${res.status}: ${err}` };
  }

  return { success: true };
}

// === Edge Function 엔트리포인트 ===
Deno.serve(async (req) => {
  const { videoId, sourceUrl } = await req.json();

  if (!videoId) {
    return new Response(JSON.stringify({ error: "videoId required" }), {
      status: 400,
    });
  }

  const result = await triggerTranscodeJob(videoId, sourceUrl);

  return new Response(JSON.stringify(result), {
    status: result.success ? 200 : 500,
    headers: { "Content-Type": "application/json" },
  });
});
```

### 호출 방법

```typescript
// Supabase 클라이언트에서
const { data, error } = await supabase.functions.invoke("trigger-transcode", {
  body: { videoId: "abc123-def456-..." },
});

// 또는 외부 URL이 필요한 경우
const { data, error } = await supabase.functions.invoke("trigger-transcode", {
  body: {
    videoId: "abc123-def456-...",
    sourceUrl: "https://other-storage.com/video.mp4",
  },
});
```

---

## 2. 트랜스코더 → Supabase 콜백

트랜스코딩 완료/실패 시 트랜스코더가 Supabase Edge Function으로 콜백합니다.

### 콜백 엔드포인트

```
POST {SUPABASE_URL}/functions/v1/transcode-callback
Authorization: Bearer {SUPABASE_ANON_KEY}
Content-Type: application/json
```

### 콜백 타입

#### 1. HLS 준비 완료 (720p)

유저가 공유 가능한 최소 품질(720p)이 준비되었을 때 발송.
이 시점에서 유저에게 "영상 공유 가능" 알림을 보낼 수 있음.

```json
{
  "type": "hls-ready",
  "videoId": "abc123-def456-...",
  "hlsUrl": "https://dev.perfectswing.app/hls/abc123-def456-.../master.m3u8",
  "isDebug": false
}
```

#### 2. HLS 전체 완료 (모든 티어)

모든 HLS 티어(360p, 720p, 1080p, 2160p) 인코딩 + 업로드가 완료되었을 때 발송.
Supabase 큐에서 해당 Job을 "완료" 처리하는 데 사용.

```json
{
  "type": "hls-complete",
  "videoId": "abc123-def456-...",
  "hlsUrl": "https://dev.perfectswing.app/hls/abc123-def456-.../master.m3u8",
  "isDebug": false
}
```

참고:
- `hls-ready`와 `hls-complete`는 항상 이 순서로 발송됨 (동기)
- 720p만 있는 영상은 `hls-ready` → `hls-complete` 연속 발송
- 여러 티어가 있으면 `hls-ready` (720p 후) → 나머지 처리 → `hls-complete` (전체 후)

#### 3. 트랜스코딩 실패

```json
{
  "type": "transcode-failed",
  "videoId": "abc123-def456-...",
  "error": "Command failed: ffmpeg... stderr: ...",
  "isDebug": false
}
```

### 콜백 처리 예시 (transcode-callback Edge Function)

```typescript
Deno.serve(async (req) => {
  const payload = await req.json();
  const { type, videoId, hlsUrl, error, isDebug } = payload;

  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  switch (type) {
    case "hls-ready":
      // DB 업데이트: HLS URL 저장 + 상태 변경
      await supabase
        .from("videos")
        .update({
          hls_url: hlsUrl,
          hls_status: "ready",
        })
        .eq("id", videoId);

      // 유저에게 알림 (push notification 등)
      // ...
      break;

    case "transcode-failed":
      await supabase
        .from("videos")
        .update({
          hls_status: "failed",
          hls_error: error,
        })
        .eq("id", videoId);
      break;
  }

  return new Response("ok");
});
```

---

## 3. 상태 관리 제안

### DB 상태 흐름

```
uploaded → hls_processing → hls_ready (720p 완료) → (이후 자동으로 고화질 추가)
                          → hls_failed
```

### 필요한 필드 (videos 테이블)

| 필드 | 타입 | 설명 |
|------|------|------|
| `hls_status` | enum | `pending`, `processing`, `ready`, `failed` |
| `hls_url` | text | master.m3u8 URL (ready 시) |
| `hls_error` | text | 에러 메시지 (failed 시) |
| `hls_triggered_at` | timestamp | 트랜스코딩 시작 시각 |

AI 분석 상태는 별도 필드로 관리:

| 필드 | 타입 | 설명 |
|------|------|------|
| `ai_status` | enum | `pending`, `processing`, `ready`, `failed` |
| `ai_result` | jsonb | AI 분석 결과 |

유저에게 "분석 완료" 알림: `hls_status = 'ready' AND ai_status = 'ready'` 둘 다 만족할 때.

---

## 4. dev/prod 환경 분리

| 항목 | Dev | Prod |
|------|-----|------|
| Supabase 프로젝트 | dev 프로젝트 | prod 프로젝트 |
| R2 버킷 | `tennis-game-video-dev` | `tennis-game-video` |
| R2 도메인 | `dev.perfectswing.app` | `perfectswing.app` |
| Cloud Run Job | `transcoder-dev` | `transcoder` |
| GCS 버킷 | `perfectswing-transcoder-scratch` | 공용 가능 |
Edge Function의 환경변수만 바꾸면 dev/prod 전환 가능.

---

## 5. 서비스 계정 키 생성 방법

Supabase Edge Function에서 사용할 서비스 계정 키:

```bash
# 서비스 계정 생성
gcloud iam service-accounts create transcoder-trigger \
  --display-name="Transcoder Trigger" \
  --project=essential-rig-491206-h9

# Cloud Run Admin 권한 부여
gcloud projects add-iam-policy-binding essential-rig-491206-h9 \
  --member="serviceAccount:transcoder-trigger@essential-rig-491206-h9.iam.gserviceaccount.com" \
  --role="roles/run.admin"

# 키 생성
gcloud iam service-accounts keys create key.json \
  --iam-account=transcoder-trigger@essential-rig-491206-h9.iam.gserviceaccount.com

# key.json 내용을 Supabase secrets에 GCP_SERVICE_ACCOUNT_KEY로 저장
```
