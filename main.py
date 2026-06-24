import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))  # 한국 표준시 (UTC+9)

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

FAILED_CACHE_FILE = "failed_videos.json"


# ──────────────────────────────────────────────
# 캐시
# ──────────────────────────────────────────────

def load_processed_cache() -> set:
    if os.path.exists(config.CACHE_FILE):
        with open(config.CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed_cache(cache: set):
    with open(config.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(cache), f, ensure_ascii=False, indent=2)


def load_failed_cache() -> dict:
    """요약 실패 영상 캐시 {video_id: attempt_count}."""
    if os.path.exists(FAILED_CACHE_FILE):
        with open(FAILED_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_failed_cache(cache: dict):
    with open(FAILED_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# YouTube
# ──────────────────────────────────────────────

def fetch_latest_videos(channel_id: str) -> list[dict]:
    """
    최근 30분 이내 업로드된 일반 영상 + 종료된 라이브 영상 조회.
    - 일반 영상: publishedAt 기준 30분
    - 라이브 영상: actualEndTime 기준 30분 (진행 중이면 제외)
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=30)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    since_kst = since.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    log.info(f"[영상 조회] 채널: {channel_id} / since: {since_kst}")

    youtube = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)

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
        log.info("[영상 조회 완료] 0건")
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
            log.info(f"[필터 제외] {title}")
            continue

        # 라이브 영상 처리
        if live_details:
            actual_end = live_details.get("actualEndTime")
            if not actual_end:
                log.info(f"[라이브 진행중 제외] {title}")
                continue
            end_dt = datetime.strptime(actual_end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if end_dt < since:
                log.info(f"[라이브 종료 30분 초과 제외] {title}")
                continue
            published_at = actual_end  # 종료 시간을 기준 시간으로 사용
            end_kst = end_dt.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
            log.info(f"[라이브 종료 확인] {title} / 종료: {end_kst}")

        videos.append({
            "id": video_id,
            "title": title,
            "url": url,
            "published_at": published_at,
        })

    log.info(f"[영상 조회 완료] {len(videos)}건 (키워드 필터 통과)")
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
    """
    Gemini API로 YouTube 영상 내용을 한국어로 요약.
    실패 시 예외를 그대로 raise — 호출부에서 처리.
    """
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
            f"출력 형식을 HTML로 해줘.\n"
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
        f"출력 형식을 HTML로 해줘.\n"
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
    published_utc = datetime.strptime(video["published_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    published_kst = published_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    return (
        f"📌 <b>[KTV 유튜브]</b>\n"
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
        f"📌 <b>[KTV 유튜브]</b>\n"
        f"📅 {published_kst}\n\n"
        f"🎬 <b>{video['title']}</b>\n"
        f"🔗 {video['url']}\n\n"
        f"⚠️ <i>요약을 가져오지 못했습니다. 영상을 직접 확인해주세요.</i>"
    )


# ──────────────────────────────────────────────
# 메인 배치 작업
# ──────────────────────────────────────────────

def run_batch():
    log.info("===== 배치 시작 =====")
    cache = load_processed_cache()
    failed_cache = load_failed_cache()
    new_count = 0

    for channel_id in config.CHANNEL_IDS:
        log.info(f"채널 수집 중: {channel_id}")
        try:
            videos = fetch_latest_videos(channel_id)
        except Exception as e:
            log.error(f"YouTube API 오류 ({channel_id}): {e}")
            continue

        for video in videos:
            video_id = video["id"]

            if video_id in cache:
                continue

            attempt = failed_cache.get(video_id, 0)
            if attempt >= 2:
                continue  # 2회 실패 → 영구 스킵

            label = "[재시도]" if attempt else "새 영상 발견"
            log.info(f"{label}: {video['title']}")

            try:
                summary = summarize_video(video["title"], video["url"])
                message = build_message(video, summary)
                send_telegram(message)
                cache.add(video_id)
                if video_id in failed_cache:
                    del failed_cache[video_id]
                new_count += 1
            except Exception as e:
                if attempt == 0:
                    # 1회차 실패: 제목+링크만 전송, 실패 기록
                    log.warning(f"요약 실패 (1차) — 제목+링크만 전송: {e}")
                    send_telegram(build_title_only_message(video))
                    failed_cache[video_id] = 1
                else:
                    # 2회차 실패: 캐시 유지, 추가 전송 없음
                    log.warning(f"요약 실패 (2차) — 재시도 종료: {e}")
                    failed_cache[video_id] = 2

            time.sleep(2)

    save_processed_cache(cache)
    save_failed_cache(failed_cache)
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
