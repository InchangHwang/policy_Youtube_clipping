# 대외정책 뉴스클리핑 — YouTube 국무회의 모니터링

YouTube KTV 채널에서 **국무회의** 영상을 자동 감지하고, Gemini AI로 요약해 Telegram으로 발송하는 AWS Lambda 서버리스 배치 시스템입니다.

---

## 실행 흐름

```
EventBridge (15분) → Lambda
    → YouTube Data API v3  : 채널 최신 영상 수집
    → 키워드 필터링         : '국무회의' 포함 영상만 통과
    → Gemini 2.5 Flash     : 영상 내용 한국어 요약
    → Telegram Bot         : 요약 메시지 발송
    → S3                   : 처리 완료 영상 ID 캐시 저장 (중복 방지)
```

---

## 수집 채널

| 채널명 | 채널 ID |
|--------|---------|
| KTV 국민방송 | `UCIMOytYIzaUpoAM2bpT4JZQ` |

> 채널 추가 시 환경변수 `CHANNEL_IDS`에 쉼표로 구분하여 입력

---

## 필터링 조건

- 영상 제목에 **`국무회의`** 또는 **`국무 회의`** 포함 시 발송
- 나머지 영상은 모두 필터 제외 (CloudWatch Logs에 기록)

---

## 파일 구조

```
policy_Youtube_clipping/
├── lambda_function.py        # Lambda 핸들러 (메인 로직)
├── main.py                   # 로컬 실행용 (테스트 전용)
├── requirements.txt          # 전체 패키지 (로컬 개발용)
├── requirements-lambda.txt   # Lambda 전용 패키지
├── config.example.py         # 환경변수 설정 예시
├── template.yaml             # SAM 인프라 참고용
├── .gitignore                # 민감파일 커밋 방지
└── scripts/
    ├── build_zip.ps1         # Lambda 배포용 zip 빌드 스크립트
    └── setup_git_secrets.sh  # git-secrets 보안 훅 설정
```

---

## AWS 배포 단계 (콘솔 수동)

### 1. Secrets Manager — API Key 등록

```
AWS 콘솔 → Secrets Manager → 새 시크릿 생성
  유형     : 다른 유형의 보안 암호 (JSON)
  이름     : policy-clipping/credentials
  리전     : ap-northeast-2 (서울)
```

저장할 JSON:
```json
{
  "YOUTUBE_API_KEY": "발급받은 YouTube Data API v3 키",
  "GEMINI_API_KEY": "발급받은 Gemini API 키",
  "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰",
  "TELEGRAM_CHAT_ID": "텔레그램 수신 채팅 ID"
}
```

---

### 2. S3 버킷 — 캐시 저장용

```
AWS 콘솔 → S3 → 버킷 만들기
  이름           : policy-clipping-cache-{AWS 계정 ID}
  리전           : ap-northeast-2 (서울)
  퍼블릭 액세스  : 모두 차단 (ON)
  버전 관리      : 활성화
```

---

### 3. IAM 역할 — Lambda 실행 권한

```
AWS 콘솔 → IAM → 역할 → 역할 만들기
  신뢰할 수 있는 엔터티 : AWS 서비스 → Lambda
  이름                  : policy-clipping-lambda-role
```

아래 권한 정책 추가:

| 정책 | 용도 |
|------|------|
| `AWSLambdaBasicExecutionRole` | CloudWatch Logs 기록 |
| `SecretsManagerReadWrite` (또는 GetSecretValue만) | API Key 조회 |
| `AmazonS3FullAccess` (또는 해당 버킷만) | 캐시 읽기/쓰기 |

---

### 4. Lambda 함수 생성

```
AWS 콘솔 → Lambda → 함수 생성
  이름     : policy-youtube-clipping
  런타임   : Python 3.12
  아키텍처 : x86_64
  실행 역할 : 3번에서 만든 역할 선택
```

**코드 업로드:**
```powershell
# 프로젝트 루트에서 PowerShell 실행
.\scripts\build_zip.ps1
# → lambda_package.zip 생성됨
```
```
Lambda 콘솔 → 코드 탭 → 업로드 위치 → .zip 파일
→ lambda_package.zip 선택 후 저장
```

**일반 구성 설정:**
```
구성 탭 → 일반 구성 → 편집
  타임아웃 : 10분 0초   (Gemini 분석 시간 고려)
  메모리   : 512 MB
```

---

### 5. 환경변수 설정

```
Lambda 콘솔 → 구성 탭 → 환경 변수 → 편집
```

| 키 | 값 |
|----|-----|
| `SECRET_NAME` | `policy-clipping/credentials` |
| `CACHE_BUCKET` | `policy-clipping-cache-{AWS 계정 ID}` |
| `CHANNEL_IDS` | `UCIMOytYIzaUpoAM2bpT4JZQ` |
| `MAX_RESULTS_PER_CHANNEL` | `10` |
| `GEMINI_MODEL` | `gemini-2.5-flash` |

---

### 6. EventBridge — 15분 주기 트리거

```
Lambda 콘솔 → 구성 탭 → 트리거 → 트리거 추가
  소스             : EventBridge (CloudWatch Events)
  규칙             : 새 규칙 생성
  규칙 이름        : policy-clipping-schedule
  규칙 유형        : 일정 표현식
  일정 표현식      : rate(15 minutes)
```

---

## 로컬 실행 (개발/테스트용)

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. config.example.py → config.py 복사 후 API Key 입력
cp config.example.py config.py

# 3. 실행
python main.py
```

---

## 보안

- `config.py`, `.env`, API Key 파일은 `.gitignore`로 커밋 차단
- API Key는 코드에 하드코딩 금지 → AWS Secrets Manager에서만 관리
- git-secrets 훅 설정으로 민감정보 자동 차단:
  ```bash
  ./scripts/setup_git_secrets.sh
  ```

---

## 주의사항

| 항목 | 내용 |
|------|------|
| Gemini API 무료 티어 | 일 20회 / 분 5회 한도 → 초과 시 다음 배치에서 재시도 |
| YouTube Data API | 일일 할당량 10,000 유닛 (검색 1회 = 100 유닛) |
| Lambda 타임아웃 | 국무회의 영상은 2~4시간 분량 → 자막 기반 요약으로 자동 전환 |
| S3 캐시 | Lambda 재배포 시 캐시 유지됨 (중복 발송 없음) |
