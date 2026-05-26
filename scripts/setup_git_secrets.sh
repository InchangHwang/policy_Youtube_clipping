#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# setup_git_secrets.sh
# AWS Access Key 등 민감 정보가 Git에 커밋되지 않도록
# git-secrets 훅을 설정하는 스크립트
#
# 사전 조건:
#   brew install git-secrets   # macOS
#   또는 https://github.com/awslabs/git-secrets#installing-git-secrets
#
# 사용법:
#   chmod +x scripts/setup_git_secrets.sh
#   ./scripts/setup_git_secrets.sh
# ─────────────────────────────────────────────────────────────────

set -e

echo "=== git-secrets 설정 시작 ==="

# git-secrets 설치 여부 확인
if ! command -v git-secrets &> /dev/null; then
    echo "[ERROR] git-secrets가 설치되어 있지 않습니다."
    echo "설치 방법:"
    echo "  macOS  : brew install git-secrets"
    echo "  Linux  : https://github.com/awslabs/git-secrets#installing-git-secrets"
    exit 1
fi

# 현재 저장소에 git-secrets 훅 설치
git secrets --install
echo "[OK] git-secrets hooks 설치 완료"

# AWS 기본 패턴 등록 (Access Key, Secret Key 탐지)
git secrets --register-aws
echo "[OK] AWS credential 패턴 등록 완료"

# 추가 금지 패턴 등록 (API Key 형식)
git secrets --add 'AIza[0-9A-Za-z\-_]{35}'          # Google/Gemini API Key
git secrets --add '[0-9]{10}:[A-Za-z0-9\-_]{35}'    # Telegram Bot Token
git secrets --add 'YOUTUBE_API_KEY\s*=\s*"[^"]+'    # YouTube API Key 하드코딩
git secrets --add 'GEMINI_API_KEY\s*=\s*"[^"]+'     # Gemini API Key 하드코딩
git secrets --add 'TELEGRAM_BOT_TOKEN\s*=\s*"[^"]+' # Telegram Token 하드코딩
echo "[OK] 추가 금지 패턴 등록 완료"

# 허용 예외 패턴 등록 (예시 파일은 허용)
git secrets --add --allowed 'YOUR_.*_KEY'
git secrets --add --allowed 'YOUR_.*_TOKEN'
git secrets --add --allowed 'config\.example\.py'
echo "[OK] 허용 예외 패턴 등록 완료"

echo ""
echo "=== git-secrets 설정 완료 ==="
echo ""
echo "이제 커밋 시 민감 정보가 포함되면 자동으로 차단됩니다."
echo "기존 히스토리 스캔: git secrets --scan-history"
