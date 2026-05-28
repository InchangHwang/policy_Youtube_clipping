# ─────────────────────────────────────────────────────────────────
# build_zip.ps1
# AWS Lambda 콘솔 수동 업로드용 zip 패키지 생성 스크립트
#
# 사용법:
#   PowerShell 열고 프로젝트 루트에서 실행:
#   .\scripts\build_zip.ps1
#
# 결과물:
#   lambda_package.zip  →  AWS 콘솔에서 이 파일을 업로드
# ─────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$BuildDir   = Join-Path $ProjectDir "build"
$ZipPath    = Join-Path $ProjectDir "lambda_package.zip"
$ReqFile    = Join-Path $ProjectDir "requirements-lambda.txt"

Write-Host ""
Write-Host "=== Lambda 패키지 빌드 시작 ===" -ForegroundColor Cyan

# ── 1. 기존 빌드 디렉토리 초기화 ──────────────────────────────
Write-Host "[1/4] 빌드 디렉토리 초기화 중..." -ForegroundColor Yellow
if (Test-Path $BuildDir) {
    Remove-Item $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildDir | Out-Null

# ── 2. Lambda 의존성 설치 ──────────────────────────────────────
Write-Host "[2/4] 의존성 설치 중 (시간이 걸릴 수 있어요)..." -ForegroundColor Yellow
pip install `
    -r $ReqFile `
    --target $BuildDir `
    --no-user `
    --quiet `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary=:all:

# ── 3. Lambda 핸들러 복사 ──────────────────────────────────────
Write-Host "[3/4] lambda_function.py 복사 중..." -ForegroundColor Yellow
Copy-Item (Join-Path $ProjectDir "lambda_function.py") $BuildDir

# ── 4. zip 압축 ────────────────────────────────────────────────
Write-Host "[4/4] zip 파일 생성 중..." -ForegroundColor Yellow
if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
Compress-Archive -Path "$BuildDir\*" -DestinationPath $ZipPath -CompressionLevel Optimal

# ── 결과 출력 ──────────────────────────────────────────────────
$ZipSizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)

Write-Host ""
Write-Host "=== 빌드 완료 ===" -ForegroundColor Green
Write-Host "파일 경로 : $ZipPath"
Write-Host "파일 크기 : $ZipSizeMB MB"

if ($ZipSizeMB -gt 50) {
    Write-Host ""
    Write-Host "[주의] zip이 50MB 초과 → 콘솔 직접 업로드 불가" -ForegroundColor Red
    Write-Host "아래 방법으로 S3 경유 업로드 하세요:" -ForegroundColor Yellow
    Write-Host "  1) aws s3 cp lambda_package.zip s3://<버킷명>/"
    Write-Host "  2) Lambda 콘솔 → 코드 업로드 → Amazon S3 위치 선택"
} else {
    Write-Host ""
    Write-Host "[안내] Lambda 콘솔 업로드 순서:" -ForegroundColor Cyan
    Write-Host "  1) AWS 콘솔 → Lambda → 함수 선택"
    Write-Host "  2) 코드 탭 → 업로드 위치 → .zip 파일"
    Write-Host "  3) lambda_package.zip 선택 후 저장"
}

Write-Host ""
