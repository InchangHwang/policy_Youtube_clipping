import json
import logging
import os
import time
from datetime import datetime, timezone

import google.generativeai as genai
import requests
import schedule
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("clipping.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 캐시
# ──────────────────────────────────────────────

def load_cache() -> set:
    if os.path.exists(config.CACHE_FILE):
        with open(config.CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_cache(cache: set):
    with open(config.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(cache), f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

def fetch_latest_videos(channel_id: str) -> list[dict]:
    """채널의 최신 영상 목록(제목 + URL) 반환."""
    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)
    response = (
        youtube.search()
        .list(
            channelId=channel_id,
            part="snippet",
            order="date",
            type="video",
            maxResults=config.MAX_RESULTS_PER_CHANNEL,
        )
        .execute()
    )

    videos = []
    for item in response.get("items", []):
        video_id = item["id"]["videoId"]
        title = item["snippet"]["title"]
        published_at = item["snippet"]["publishedAt"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        videos.append(
                {"id": video_id, "title": title, "url": url, "published_at": published_at}
            )
    return videos


# ──────────────────────────────────────────────
# Gemini 요약
# ──────────────────────────────────────────────

def get_transcript(video_id: str) -> str:
    """YouTube 자막을 가져와 텍스트로 반환."""
    api = YouTubeTranscriptApi()
    transcript = api.fetch(video_id, languages=["ko", "en"])
    text = " ".join([t.text for t in transcript])
    # Gemini 입력 토큰 한도 고려해 앞 15만 자 제한
    return text[:150000]


def summarize_video(title: str, url: str) -> str:
    """Gemini API로 YouTube 영상 내용을 한국어로 요약."""
    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(config.GEMINI_MODEL)
    video_id = url.split("v=")[-1]

    summary_prompt = (
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
            + summary_prompt
        )
        response = model.generate_content(
            [prompt, {"file_data": {"mime_type": "video/*", "file_uri": url}}]
        )
        return response.text.strip()
    except Exception as e:
        log.warning(f"영상 직접 분석 실패, 자막 기반 요약으로 전환: {e}")

    # 2차 시도: 자막 텍스트 기반 분석
    transcript = get_transcript(video_id)
    prompt = (
        f"다음은 유튜브 영상의 자막 텍스트야. 한국어로 요약해줘.\n"
        f"영상 제목: {title}\n\n"
        f"[자막]\n{transcript}\n\n"
        + summary_prompt
    )
    response = model.generate_content(prompt)
    return response.text.strip()


# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────

def send_telegram(message: str, retries: int = 5, retry_delay: int = 10):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.ok:
                log.info("텔레그램 전송 완료")
                return
            else:
                log.warning(f"텔레그램 전송 실패 ({attempt}/{retries}): {resp.status_code} {resp.text}")
        except Exception as e:
            log.warning(f"텔레그램 연결 오류 ({attempt}/{retries}): {e}")
        if attempt < retries:
            log.info(f"{retry_delay}초 후 재시도...")
            time.sleep(retry_delay)
    log.error("텔레그램 전송 최종 실패 — 다음 배치에서 재시도됩니다.")


def build_message(video: dict, summary: str) -> str:
    published = video["published_at"][:10]
    return (
        f"📌 <b>[대외정책 뉴스클리핑]</b>\n"
        f"📅 {published}\n\n"
        f"🎬 <b>{video['title']}</b>\n"
        f"🔗 {video['url']}\n\n"
        f"📝 <b>요약</b>\n{summary}"
    )


# ──────────────────────────────────────────────
# 메인 배치 작업
# ──────────────────────────────────────────────

def run_batch():
    log.info("===== 배치 시작 =====")
    cache = load_cache()
    new_count = 0

    for channel_id in config.CHANNEL_IDS:
        log.info(f"채널 수집 중: {channel_id}")
        try:
            videos = fetch_latest_videos(channel_id)
        except Exception as e:
            log.error(f"YouTube API 오류 ({channel_id}): {e}")
            continue

        for video in videos:
            if video["id"] in cache:
                continue

            log.info(f"새 영상 발견: {video['title']}")
            try:
                summary = summarize_video(video["title"], video["url"])
                message = build_message(video, summary)
                send_telegram(message)
                cache.add(video["id"])
                new_count += 1
                time.sleep(2)  # API 요청 간격
            except Exception as e:
                log.error(f"처리 실패 ({video['title']}): {e}")

    save_cache(cache)
    log.info(f"===== 배치 완료 (신규 {new_count}건) =====")


# ──────────────────────────────────────────────
# 스케줄러
# ──────────────────────────────────────────────

if __name__ == "__main__":
    log.info(f"뉴스클리핑 봇 시작 — {config.CHECK_INTERVAL_MINUTES}분 간격")
    run_batch()  # 시작 시 즉시 1회 실행
    schedule.every(config.CHECK_INTERVAL_MINUTES).minutes.do(run_batch)

    while True:
        schedule.run_pending()
        time.sleep(30)
