"""
대외정책 뉴스클리핑 - YouTube KTV 정책뉴스 모니터링
AWS Lambda 핸들러

[ 실행 흐름 ]
EventBridge (15분) → lambda_handler
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
from youtube_transcript_api import YouTubeTranscriptApi

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


# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

def fetch_latest_videos(channel_id: str, api_key: str) -> list[dict]:
    """
    최근 30분 이내 업로드된 일반 영상 + 종료된 라이브 영상 조회.
    - 일반 영상: publishedAt 기준 30분
    - 라이브 영상: actualEndTime 기준 30분 (진행 중이면 제외)
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=30)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_kst = since.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    logger.info(f"[영상 조회] 채널: {channel_id} / since: {since_kst}")

    youtube = build(
        "youtube", "v3",
        developerKey=api_key,
        cache_discovery=False,
    )

    # 1. 최근 30분 내 업로드된 영상
    regular_resp = youtube.search().list(
        channelId=channel_id,
        part="snippet",
        order="date",
        type="video",
        publishedAfter=since_str,
        maxResults=10,
    ).execute()

    # 2. 최근 완료된 라이브 스트림 (종료 시점 기준 수집)
    live_resp = youtube.search().list(
        channelId=channel_id,
        part="snippet",
        order="date",
        type="video",
        eventType="completed",
        maxResults=5,
    ).execute()

    # 중복 제거 병합
    all_items: dict = {}
    for item in regular_resp.get("items", []) + live_resp.get("items", []):
        vid_id = item["id"]["videoId"]
        if vid_id not in all_items:
            all_items[vid_id] = item

    if not all_items:
        logger.info("[영상 조회 완료] 0건")
        return []

    # liveStreamingDetails 조회 (라이브 종료 여부 확인)
    details_resp = youtube.videos().list(
        id=",".join(all_items.keys()),
        part="liveStreamingDetails,snippet",
    ).execute()

    KEYWORDS = ["국무회의", "국무 회의", "수석보좌관회의"]
    videos = []

    for detail in details_resp.get("items", []):
        video_id = detail["id"]
        title = detail["snippet"]["title"]
        published_at = detail["snippet"]["publishedAt"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        live_details = detail.get("liveStreamingDetails")

        # 키워드 필터링
        if not any(kw in title for kw in KEYWORDS):
            logger.info(f"[필터 제외] {title}")
            continue

        # 라이브 영상 처리
        if live_details:
            actual_end = live_details.get("actualEndTime")
            if not actual_end:
                logger.info(f"[라이브 진행중 제외] {title}")
                continue
            end_dt = datetime.strptime(actual_end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if end_dt < since:
                logger.info(f"[라이브 종료 30분 초과 제외] {title}")
                continue
            published_at = actual_end  # 종료 시간을 기준 시간으로 사용
            end_kst = end_dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
            logger.info(f"[라이브 종료 확인] {title} / 종료: {end_kst}")

        videos.append({
            "id": video_id,
            "title": title,
            "url": url,
            "published_at": published_at,
        })

    logger.info(f"[영상 조회 완료] {len(videos)}건 (키워드 필터 통과)")
    return videos


# ──────────────────────────────────────────────
# Gemini 요약
# ──────────────────────────────────────────────

def get_transcript(video_id: str) -> str:
    """YouTube 자막 텍스트 추출 (한국어 → 영어 순)."""
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["ko", "en"])
    text = " ".join([t.text for t in transcript])
    return text[:150000]  # Gemini 입력 토큰 한도 대비


def summarize_video(title: str, url: str, gemini_api_key: str) -> str:
    """
    Gemini API로 영상 내용을 한국어로 요약.
    1차: 영상 URL 직접 분석
    2차: 자막 텍스트 기반 분석 (영상이 너무 길 경우)
    실패 시 예외를 그대로 raise — 호출부에서 처리.
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(
        os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    )
    video_id = url.split("v=")[-1]

    summary_format = (
        "요약 형식:\n"
        "• 핵심 주제 1~2문장\n"
        "• 주요 내용 bullet 3~5개\n"
        "• 대외정책 관련 시사점 (있을 경우)\n"
    )

    # 1차 시도: 영상 직접 분석
    try:
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
    except Exception as e:
        logger.warning(f"영상 직접 분석 실패, 자막 기반 요약으로 전환: {e}")

    # 2차 시도: 자막 텍스트 기반 분석
    transcript = get_transcript(video_id)
    prompt = (
        f"다음은 유튜브 영상의 자막 텍스트야. 한국어로 요약해줘.\n"
        f"영상 제목: {title}\n\n"
        f"[자막]\n{transcript}\n\n"
        + summary_format
    )
    response = model.generate_content(prompt)
    logger.info("Gemini 자막 기반 분석 완료")
    return response.text.strip()


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
    """
    AWS Lambda 진입점.
    EventBridge에 의해 15분마다 자동 호출됨.
    """
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
    failed_cache = load_failed_cache()
    new_count = 0

    for channel_id in channel_ids:
        logger.info(f"채널 수집 중: {channel_id}")
        try:
            videos = fetch_latest_videos(channel_id, youtube_api_key)
        except Exception as e:
            logger.error(f"YouTube API 오류 ({channel_id}): {e}")
            continue

        for video in videos:
            video_id = video["id"]

            if video_id in processed_cache:
                continue

            attempt = failed_cache.get(video_id, 0)
            if attempt >= 2:
                continue  # 2회 실패 → 영구 스킵

            label = "[재시도]" if attempt else "새 영상 발견"
            logger.info(f"{label}: {video['title']}")

            try:
                summary = summarize_video(video["title"], video["url"], gemini_api_key)
                message = build_message(video, summary)
                send_telegram(message, telegram_bot_token, telegram_chat_id)
                processed_cache.add(video_id)
                if video_id in failed_cache:
                    del failed_cache[video_id]
                new_count += 1
            except Exception as e:
                if attempt == 0:
                    # 1회차 실패: 제목+링크만 전송, 실패 기록
                    logger.warning(f"요약 실패 (1차) — 제목+링크만 전송: {e}")
                    send_telegram(build_title_only_message(video), telegram_bot_token, telegram_chat_id)
                    failed_cache[video_id] = 1
                else:
                    # 2회차 실패: 캐시 유지, 추가 전송 없음
                    logger.warning(f"요약 실패 (2차) — 재시도 종료: {e}")
                    failed_cache[video_id] = 2

            time.sleep(2)

    save_processed_cache(processed_cache)
    save_failed_cache(failed_cache)
    logger.info(f"===== Lambda 배치 완료 (신규 {new_count}건) =====")

    return {
        "statusCode": 200,
        "body": json.dumps({"new_videos": new_count}),
    }
