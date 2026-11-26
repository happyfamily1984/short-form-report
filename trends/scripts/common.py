"""공통 도구: 설정 읽기, 날짜(한국 시간), 경로."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent          # trends/
DATA = ROOT / "data"
CONFIG_PATH = ROOT / "config.yaml"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def today_kst():
    """실행 날짜(한국 시간). TRENDS_DATE 환경변수로 바꿀 수 있음(시험용)."""
    return os.environ.get("TRENDS_DATE") or datetime.now(KST).strftime("%Y-%m-%d")


def day_dir(date):
    d = DATA / date
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_status(date, step, ok, message=""):
    """단계별 성공/실패를 status.json에 기록 (텔레그램 실패 알림용)."""
    path = day_dir(date) / "status.json"
    status = read_json(path) if path.exists() else {}
    status[step] = {"ok": ok, "message": message[:300]}
    write_json(path, status)


_SECRET_PATTERNS = [
    re.compile(r"bot\d+:[A-Za-z0-9_-]+"),     # 텔레그램 토큰
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),     # 구글 API 키
    re.compile(r"(key=)[^&\s]+"),
]


def scrub(text):
    """오류 메시지에서 비밀값처럼 보이는 부분을 가린다."""
    text = str(text)
    for env in ("YOUTUBE_API_KEY", "GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN"):
        v = os.environ.get(env)
        if v:
            text = text.replace(v, "***")
    for p in _SECRET_PATTERNS:
        text = p.sub("***", text)
    return text


def fmt_num(n):
    """12345 -> 1.2만"""
    n = int(n or 0)
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}억"
    if n >= 10_000:
        return f"{n / 10_000:.1f}만"
    return f"{n:,}"


def fmt_dur(sec):
    sec = int(sec or 0)
    return f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"
