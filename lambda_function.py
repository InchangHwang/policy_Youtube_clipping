"""
대외정책 뉴스클리핑 - YouTube 국무회의 모니터링
AWS Lambda 핸들러

[ 실행 흐름 ]
EventBridge (15분) → lambda_handler
    → Secrets Manager에서 API Key 조회
    → YouTube API로 채널 최신 영상 수집
    → 국무회의 키워드 필터링
    → Gemini API로 영상 요약 (직접 분석 → 자막 분석 순)
    → 텔레그램 전송
    → S3 캐시 저장 (중복 방지)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

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
# S3 캐시 (처리된 영상 ID 저장 — Lambda 무상태 보완)
# ──────────────────────────────────────────────

def load_cache() -> set:
    """S3에서 처리된 영상 ID 캐시 로드."""
    s3 = boto3.client("s3")
    bucket = os.environ["CACHE_BUCKET"]
    try:
        resp = s3.get_object(Bucket=bucket, Key="processed_videos.json")
        return set(json.loads(resp["Body"].read().decode("utf-8")))
    except s3.exceptions.NoSuchKey:
        logger.info("캐시 파일 없음 — 신규 시작")
        return set()
    except Exception as e:
        logger.warning(f"캐시 로드 실패 (빈 캐시로 진행): {e}")
        return set()


def save_cache(cache: set):
    """처리된 영상 ID 캐시를 S3에 저장."""
    s3 = boto3.client("s3")
    bucket = os.environ["CACHE_BUCKET"]
    try:
        s3.put_object(
            Bucket=bucket,
            Key="processed_videos.json",
            Body=json.dumps(list(cache), ensure_ascii=False),
            ContentType="application/json",
        )
    except Exception as e:
        logger.error(f"캐시 저장 실패: {e}")


# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

def fetch_latest_videos(channel_id: str, api_key: str) -> list[dict]:
    """최근 30분 이내 업로드된 영상만 조회."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=30)
    published_after = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(f"[영상 조회] 채널: {channel_id} / since: {published_after}")

    youtube = build(
        "youtube", "v3",
        developerKey=api_key,
        cache_discovery=False,
    )
    response = (
        youtube.search()
        .list(
            channelId=channel_id,
            part="snippet",
            order="date",
            type="video",
            publishedAfter=published_after,
            maxResults=10,
        )
        .execute()
    )

    videos = []
    for item in response.get("items", []):
        video_id = item["id"]["videoId"]
        title = item["snippet"]["title"]
        videos.append({
            "id": video_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "published_at": item["snippet"]["publishedAt"],
        })

    logger.info(f"[영상 조회 완료] {len(videos)}건")
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
    published = video["published_at"][:10]
    return (
        f"📌 <b>[대외정책 뉴스클리핑]</b>\n"
        f"📅 {published}\n\n"
        f"🎬 <b>{video['title']}</b>\n"
        f"🔗 {video['url']}\n\n"
        f"📝 <b>요약</b>\n{summary}"
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

    # Secrets Manager에서 API Key 조회
    secrets = get_secrets()
    youtube_api_key    = secrets["YOUTUBE_API_KEY"]
    gemini_api_key     = secrets["GEMINI_API_KEY"]
    telegram_bot_token = secrets["TELEGRAM_BOT_TOKEN"]
    telegram_chat_id   = secrets["TELEGRAM_CHAT_ID"]

    # 환경 변수에서 채널 ID 목록 조회
    channel_ids = [
        cid.strip()
        for cid in os.environ.get("CHANNEL_IDS", "").split(",")
        if cid.strip()
    ]

    cache = load_cache()
    new_count = 0

    for channel_id in channel_ids:
        logger.info(f"채널 수집 중: {channel_id}")
        try:
            videos = fetch_latest_videos(channel_id, youtube_api_key)
        except Exception as e:
            logger.error(f"YouTube API 오류 ({channel_id}): {e}")
            continue

        for video in videos:
            if video["id"] in cache:
                continue

            logger.info(f"새 영상 발견: {video['title']}")
            try:
                summary = summarize_video(
                    video["title"], video["url"], gemini_api_key
                )
                message = build_message(video, summary)
                send_telegram(message, telegram_bot_token, telegram_chat_id)
                cache.add(video["id"])
                new_count += 1
                time.sleep(2)
            except Exception as e:
                logger.error(f"처리 실패 ({video['title']}): {e}")

    save_cache(cache)
    logger.info(f"===== Lambda 배치 완료 (신규 {new_count}건) =====")

    return {
        "statusCode": 200,
        "body": json.dumps({"new_videos": new_count}),
    }
