"""
대외정책 뉴스클리핑 - YouTube KTV 정책뉴스 모니터링
AWS Lambda 핸들러

[ 실행 흐름 ]
EventBridge (60분) → lambda_handler
    → Secrets Manager에서 API Key 조회
    → YouTube API로 채널 최신 영상 수집 (일반 + 완료된 라이브)
    → 키워드 필터링 (국무회의 / 수석보좌관회의)
    → 라이브 영상: actualEndTime 확인 → 진행 중이면 제외
    → Gemini API로 영상 요약 (직접 분석 → 자막 분석 순)
      → 요약 실패 시: 제목+링크만 전송, S3에 실패 기록
      → 30분 후 재시도: 성공 시 전송, 실패 시 캐시 유지
    → 텔레그램 전송
    → S3 캐시 저장 (중복 방지)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))  # 한국 표준시 (UTC+9)

import boto3
import google.generativeai as genai
import requests
from botocore.exceptions import ClientError
from googleapiclient.discovery import build

# ──────────────────────────────────────────────
# 로깅 (CloudWatch Logs 자동 연동)
# ──────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda 컨테이너 재사용 시 Secrets 캐싱 (콜드 스타트 최적화)
_secrets_cache: dict | None = None


# ──────────────────────────────────────────────
# AWS Secrets Manager
# ──────────────────────────────────────────────

def get_secrets() -> dict:
    """Secrets Manager에서 API Key 조회. 컨테이너 재사용 시 메모리 캐시 활용."""
    global _secrets_cache
    if _secrets_cache:
        return _secrets_cache

    secret_name = os.environ["SECRET_NAME"]
    region = os.environ.get("AWS_REGION", "ap-northeast-2")

    client = boto3.client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
        _secrets_cache = json.loads(resp["SecretString"])
        logger.info("Secrets Manager 조회 완료")
        return _secrets_cache
    except ClientError as e:
        logger.error(f"Secrets Manager 조회 실패: {e}")
        raise


# ──────────────────────────────────────────────
# S3 캐시 (Lambda 무상태 보완)
# ──────────────────────────────────────────────

def _s3_get_json(key: str, default):
    s3 = boto3.client("s3")
    bucket = os.environ["CACHE_BUCKET"]
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        return default
    except Exception as e:
        logger.warning(f"S3 읽기 실패 ({key}): {e}")
        return default


def _s3_put_json(key: str, data):
    s3 = boto3.client("s3")
    bucket = os.environ["CACHE_BUCKET"]
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False),
            ContentType="application/json",
        )
    except Exception as e:
        logger.error(f"S3 저장 실패 ({key}): {e}")


def load_processed_cache() -> set:
    """처리 완료(전송 성공) 영상 ID 목록."""
    return set(_s3_get_json("processed_videos.json", []))


def save_processed_cache(cache: set):
    _s3_put_json("processed_videos.json", list(cache))


def load_failed_cache() -> dict:
    """요약 실패 영상 캐시 {video_id: attempt_count}."""
    return _s3_get_json("failed_videos.json", {})


def save_failed_cache(cache: dict):
    _s3_put_json("failed_videos.json", cache)

def load_pending_cache() -> dict:
    """라이브/예정 영상 대기 캐시 {video_id: {title, url}}."""
    return _s3_get_json("pending_live_videos.json", {})

def save_pending_cache(cache: dict):
    _s3_put_json("pending_live_videos.json", cache)

# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

def fetch_latest_videos(channel_id: str, api_key: str) -> list[dict]:
    """최근 1시간 이내 업로드된 영상 조회."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_kst = since.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    logger.info(f"[영상 조회] 채널: {channel_id} / since: {since_kst}")
    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    response = youtube.search().list(
        channelId=channel_id,
        part="snippet",
        order="date",
        type="video",
        publishedAfter=since_str,
        maxResults=10,
    ).execute()
    KEYWORDS = ["국무회의", "국무 회의", "수석보좌관회의"]
    logger.info(f"[키워드 필터] 조회 키워드: {KEYWORDS}")
    videos = []
    for item in response.get("items", []):
        video_id = item["id"]["videoId"]
        title = item["snippet"]["title"]
        published_at = item["snippet"]["publishedAt"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        live_status = item["snippet"].get("liveBroadcastContent", "none")
        matched = [kw for kw in KEYWORDS if kw in title]
        if not matched:
            logger.info(f"[필터 제외] {title}")
            continue
        logger.info(f"[키워드 일치] '{matched}' / 상태: {live_status} → {title}")
        videos.append({
            "id": video_id,
            "title": title,
            "url": url,
            "published_at": published_at,
            "live_status": live_status,
        })
    logger.info(f"[영상 조회 완료] {len(videos)}건")
    return videos


def check_pending_lives(
    pending: dict,
    api_key: str,
    gemini_api_key: str,
    bot_token: str,
    chat_id: str,
    processed_cache: set,
    failed_cache: dict,
) -> int:
    """대기 중인 라이브 영상들의 종료 여부를 확인하고, 종료된 영상은 요약 후 전송."""
    if not pending:
        return 0
    youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    details_resp = youtube.videos().list(
        id=",".join(pending.keys()),
        part="liveStreamingDetails,snippet",
    ).execute()
    sent = 0
    completed_ids = []
    for detail in details_resp.get("items", []):
        video_id = detail["id"]
        info = pending.get(video_id, {})
        title = info.get("title", detail["snippet"]["title"])
        url = info.get("url", f"https://www.youtube.com/watch?v={video_id}")
        actual_end = detail.get("liveStreamingDetails", {}).get("actualEndTime")
        if actual_end:
            logger.info(f"[라이브 종료 확인] {title}")
            video_data = {"title": title, "url": url, "published_at": actual_end}
            attempt = failed_cache.get(video_id, 0)
            try:
                summary = summarize_video(title, url, gemini_api_key, youtube_api_key)
                send_telegram(build_message(video_data, summary), bot_token, chat_id)
                processed_cache.add(video_id)
                failed_cache.pop(video_id, None)
                sent += 1
            except Exception as e:
                if attempt == 0:
                    logger.warning(f"요약 실패 (1차): {e}")
                    send_telegram(build_title_only_message(video_data), bot_token, chat_id)
                    failed_cache[video_id] = 1
                else:
                    logger.warning(f"요약 실패 (2차): {e}")
                    failed_cache[video_id] = 2
            completed_ids.append(video_id)
        else:
            logger.info(f"[라이브 진행중 — 다음 차시 재시도] {title}")
    for vid_id in completed_ids:
        pending.pop(vid_id, None)
    return sent

# ──────────────────────────────────────────────
# Gemini 요약
# ──────────────────────────────────────────────

def get_transcript(video_id: str) -> str:
    """YouTube 자막 텍스트 추출 (한국어 → 영어 순)."""
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["ko", "en"])
    text = " ".join([t.text for t in transcript])
    return text[:150000]  # Gemini 입력 토큰 한도 대비

import re
def parse_iso_duration(duration: str) -> int:
    """ISO 8601 → 초 변환. 예: PT1H23M45S → 5025"""
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not match:
        return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s

def summarize_video(title: str, url: str, gemini_api_key: str, youtube_api_key: str) -> str:
    """
    1) videos.list로 description 가져와서 요약
    2) description 없으면 영상 길이 확인 → 1시간 이하면 Gemini 직접 분석
    3) 둘 다 안 되면 예외 발생 → 호출부에서 제목+링크 전송
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))
    video_id = url.split("v=")[-1]
    summary_format = (
        "요약 형식:\n"
        "• 핵심 주제 1~2문장\n"
        "• 주요 내용 bullet 3~5개\n"
        "• 대외정책 관련 시사점 (있을 경우)\n"
    )
    # videos.list로 description + duration 조회
    youtube = build("youtube", "v3", developerKey=youtube_api_key, cache_discovery=False)
    resp = youtube.videos().list(id=video_id, part="snippet,contentDetails").execute()
    items = resp.get("items", [])
    description = ""
    duration_seconds = 0
    if items:
        description = items[0]["snippet"].get("description", "").strip()
        duration_iso = items[0]["contentDetails"].get("duration", "PT0S")
        duration_seconds = parse_iso_duration(duration_iso)
        logger.info(f"[영상 정보] description 길이: {len(description)}자 / 길이: {duration_seconds}초")
    # 1차 시도: description 기반 요약
    if description:
        logger.info("Gemini description 기반 요약 시도")
        prompt = (
            f"다음은 유튜브 영상의 설명(description)이야. 한국어로 요약해줘.\n"
            f"영상 제목: {title}\n\n"
            f"[영상 설명]\n{description}\n\n"
            + summary_format
        )
        response = model.generate_content(prompt)
        logger.info("Gemini description 기반 요약 완료")
        return response.text.strip()
    # 2차 시도: 영상 1시간 이하면 Gemini 직접 분석
    if duration_seconds <= 3600:
        logger.info(f"Gemini 영상 직접 분석 시도 ({duration_seconds}초)")
        prompt = (
            f"다음 유튜브 영상을 한국어로 요약해줘.\n"
            f"영상 제목: {title}\n"
            f"영상 URL: {url}\n\n"
            + summary_format
        )
        response = model.generate_content(
            [prompt, {"file_data": {"mime_type": "video/*", "file_uri": url}}]
        )
        logger.info("Gemini 영상 직접 분석 완료")
        return response.text.strip()
    # 3차: 요약 불가 → 예외 발생
    raise Exception(f"요약 불가 — description 없음, 영상 {duration_seconds//60}분 초과")


# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────

def build_message(video: dict, summary: str) -> str:
    published_utc = datetime.strptime(video["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    published_kst = published_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    return (
        f"📌 <b>[KTV 정책뉴스]</b>\n"
        f"📅 {published_kst}\n\n"
        f"🎬 <b>{video['title']}</b>\n"
        f"🔗 {video['url']}\n\n"
        f"📝 <b>요약</b>\n{summary}"
    )


def build_title_only_message(video: dict) -> str:
    """요약 실패 시 영상 제목과 링크만 전송."""
    published_utc = datetime.strptime(video["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    published_kst = published_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    return (
        f"📌 <b>[KTV 정책뉴스]</b>\n"
        f"📅 {published_kst}\n\n"
        f"🎬 <b>{video['title']}</b>\n"
        f"🔗 {video['url']}\n\n"
        f"⚠️ <i>요약을 가져오지 못했습니다. 영상을 직접 확인해주세요.</i>"
    )


def send_telegram(
    message: str,
    bot_token: str,
    chat_id: str,
    retries: int = 5,
    retry_delay: int = 10,
):
    """텔레그램 메시지 전송. 실패 시 최대 5회 재시도."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.ok:
                logger.info("텔레그램 전송 완료")
                return
            logger.warning(
                f"텔레그램 전송 실패 ({attempt}/{retries}): "
                f"{resp.status_code} {resp.text}"
            )
        except Exception as e:
            logger.warning(f"텔레그램 연결 오류 ({attempt}/{retries}): {e}")
        if attempt < retries:
            logger.info(f"{retry_delay}초 후 재시도...")
            time.sleep(retry_delay)
    logger.error("텔레그램 전송 최종 실패 — 다음 실행에서 재시도됩니다.")


# ──────────────────────────────────────────────
# Lambda Entry Point
# ──────────────────────────────────────────────

def lambda_handler(event, context):
    logger.info("===== Lambda 배치 시작 =====")
    secrets = get_secrets()
    youtube_api_key    = secrets["YOUTUBE_API_KEY"]
    gemini_api_key     = secrets["GEMINI_API_KEY"]
    telegram_bot_token = secrets["TELEGRAM_BOT_TOKEN"]
    telegram_chat_id   = secrets["TELEGRAM_CHAT_ID"]
    channel_ids = [
        cid.strip()
        for cid in os.environ.get("CHANNEL_IDS", "").split(",")
        if cid.strip()
    ]
    processed_cache = load_processed_cache()
    failed_cache    = load_failed_cache()
    pending_cache   = load_pending_cache()
    new_count = 0
    # 1. 대기 중인 라이브 영상 종료 여부 먼저 확인
    new_count += check_pending_lives(
        pending_cache, youtube_api_key, gemini_api_key,
        telegram_bot_token, telegram_chat_id,
        processed_cache, failed_cache,
    )
    # 2. 새 영상 수집
    for channel_id in channel_ids:
        logger.info(f"채널 수집 중: {channel_id}")
        try:
            videos = fetch_latest_videos(channel_id, youtube_api_key)
        except Exception as e:
            logger.error(f"YouTube API 오류 ({channel_id}): {e}")
            continue
        for video in videos:
            video_id   = video["id"]
            live_status = video.get("live_status", "none")
            if video_id in processed_cache or video_id in pending_cache:
                continue
            if live_status == "upcoming":
                msg = (
                    f"📌 <b>[KTV 정책뉴스]</b>\n\n"
                    f"⏰ 잠시 후 라이브가 진행됩니다.\n\n"
                    f"🎬 <b>{video['title']}</b>\n"
                    f"🔗 {video['url']}"
                )
                send_telegram(msg, telegram_bot_token, telegram_chat_id)
                pending_cache[video_id] = {"title": video["title"], "url": video["url"]}
                logger.info(f"[라이브 예정 알림] {video['title']}")
            elif live_status == "live":
                msg = (
                    f"📌 <b>[KTV 정책뉴스]</b>\n\n"
                    f"🔴 라이브가 진행 중입니다.\n\n"
                    f"🎬 <b>{video['title']}</b>\n"
                    f"🔗 {video['url']}"
                )
                send_telegram(msg, telegram_bot_token, telegram_chat_id)
                pending_cache[video_id] = {"title": video["title"], "url": video["url"]}
                logger.info(f"[라이브 진행중 알림] {video['title']}")
            else:
                attempt = failed_cache.get(video_id, 0)
                if attempt >= 2:
                    continue
                label = "[재시도]" if attempt else "새 영상 발견"
                logger.info(f"{label}: {video['title']}")
                try:
                    summary = summarize_video(video["title"], video["url"], gemini_api_key, youtube_api_key)
                    send_telegram(build_message(video, summary), telegram_bot_token, telegram_chat_id)
                    processed_cache.add(video_id)
                    failed_cache.pop(video_id, None)
                    new_count += 1
                except Exception as e:
                    if attempt == 0:
                        logger.warning(f"요약 실패 (1차) — 제목+링크만 전송: {e}")
                        send_telegram(build_title_only_message(video), telegram_bot_token, telegram_chat_id)
                        failed_cache[video_id] = 1
                    else:
                        logger.warning(f"요약 실패 (2차) — 재시도 종료: {e}")
                        failed_cache[video_id] = 2
            time.sleep(2)
    save_processed_cache(processed_cache)
    save_failed_cache(failed_cache)
    save_pending_cache(pending_cache)
    logger.info(f"===== Lambda 배치 완료 (신규 {new_count}건) =====")
    return {
        "statusCode": 200,
        "body": json.dumps({"new_videos": new_count}),
    }
