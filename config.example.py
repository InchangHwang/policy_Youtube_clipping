# ─────────────────────────────────────────────────────
# config.example.py
# 실제 사용 시 이 파일을 config.py 로 복사한 뒤 값을 채워주세요.
# cp config.example.py config.py
# ─────────────────────────────────────────────────────

YOUTUBE_API_KEY  = "YOUR_YOUTUBE_DATA_API_KEY"
GEMINI_API_KEY   = "YOUR_GEMINI_API_KEY"
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_TELEGRAM_CHAT_ID"

# 모니터링할 유튜브 채널 ID 목록
CHANNEL_IDS = [
    "UCxxxxxxxxxxxxxxxxxxxxxxxxx",  # 채널명
]

CHECK_INTERVAL_MINUTES = 15     # 수집 주기 (분)
MAX_RESULTS_PER_CHANNEL = 10    # 채널당 최신 영상 수집 개수
CACHE_FILE = "processed_videos.json"
GEMINI_MODEL = "gemini-2.5-flash"
