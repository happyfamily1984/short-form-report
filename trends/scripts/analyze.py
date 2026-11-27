"""② 형식 분석 — 제미나이 무료 키 (google-genai).

사용법:
  python trends/scripts/analyze.py          # 실제 분석 (GEMINI_API_KEY 필요)
  python trends/scripts/analyze.py --mock   # 키 없이 가짜 응답으로 흐름 시험

요청 수: 장르 수(숏드라마 10 + 인기 숏폼 10) + 전체 요약(1) + 영상 직접 보기(watch.max_total) = 하루 약 29회
실패해도 프로그램은 멈추지 않고, 수집 요약만 담은 analysis.md를 만든 뒤
status.json에 실패 사실을 남긴다 → notify.py가 텔레그램으로 알려 줌.
"""
import argparse
import json
import re
import os
import sys
import time
from datetime import datetime, timedelta

import requests

from common import (DATA, day_dir, fmt_dur, fmt_num, load_config, read_json,
                    scrub, today_kst, write_json, write_status)

RULES = """규칙:
- 영상의 "형식"(구조, 길이, 훅 방식, 편집 리듬, 썸네일 디자인)만 분석한다.
- 제목·설명·댓글·대사를 그대로 옮겨 적지 않는다. 반드시 내 말로 요약한다 (따옴표 인용 금지).
- 영상 본편을 직접 본 것이 아니므로, 제목·설명·댓글·길이·썸네일로 추정한 내용은 끝에 "(추정)"을 붙인다.
- 모든 답은 쉬운 한국어로, 항목마다 1~2문장으로 짧게.
- cast(등장인물 0~4명), flow(결말 직전까지 3~5단계), ending(결말)은 다른 사람이 인물·사건·결말만 바꿔 같은 흐름의 새 대본을 쓸 수 있게 구체적으로 나눠 적는다. 실명·대사 인용은 쓰지 않는다.
- cast 는 사람만이 아니다. 동물이면 kind 를 "동물", 애니메이션·그림·3D 캐릭터면 "캐릭터", 물건·음식 같은 것이 주인공이면 "사물"로 적는다. 등장인물이 없는 영상(풍경·글자·손만 나오는 영상 등)은 cast 를 빈 목록 [] 으로 둔다.
- cast 의 look 은 그림으로 다시 그릴 수 있게 외형만 적는다. 사람: 나이대·성별·머리·옷·인상 / 동물: 종·털색·크기·특징 / 캐릭터: 그림체·색·생김새·특징. 실존 인물 이름, 기존 작품의 캐릭터 이름·상표는 쓰지 않고 생김새로만 적는다.
- script 는 장면 순서대로 4~8줄. 줄마다 화면에 보이는 것(scene)과 그 장면의 말·내레이션·자막의 요지(line)를 내 말로 적는다. 말이 없는 장면은 line 을 빈 문자열로 둔다.
- 반드시 지정한 JSON 형식으로만 답한다."""

GENRE_SCHEMA = """{
  "videos": [
    {
      "video_id": "영상 ID 그대로",
      "title_ko": "해외 영상만: 제목을 자연스러운 한국어로 번역 (한국 영상은 빈 문자열)",
      "hook": "제목 훅 방식 (예: 반전 예고, 대사 인용형, 질문형, 숫자형)",
      "structure": "추정 이야기 구조 (예: 무시 → 반전 → 사이다)",
      "format": "영상 종류 한 단어 (숏드라마/상황극/동물/애니메이션/썰·사연/정보/챌린지/기타)",
      "audio": "소리 방식 (인물 대사 / 내레이션 / 자막만 / 음악만)",
      "cast": [{"role": "역할 (주인공/악역/조력자/구경꾼 등)", "kind": "사람/동물/캐릭터/사물 중 하나", "who": "이 영상 속 인물 설정 한 줄 (나이대·직업·처지·숨은 정체, 동물·캐릭터면 성격과 처지)", "look": "외형 한 줄 (그림으로 다시 그릴 수 있게)"}],
      "script": [{"t": "0~3초", "scene": "화면에 보이는 것 한 줄", "line": "그 장면의 말·내레이션·자막 요지 (없으면 빈 문자열)"}],
      "flow": ["이야기 흐름 1단계 한 줄", "2단계", "3단계 (결말 직전까지 3~5단계)"],
      "ending": "결말 한 줄 (누가 어떻게 응징·역전되며 끝나는지)",
      "thumbnail": "썸네일 특징 (표정, 글자, 색). 이미지를 못 받은 영상은 제목 기준 추정",
      "reaction": "댓글에서 사람들이 반응한 지점 (요약)",
      "series": "시리즈 여부 (회차 번호, 재생목록·다음편 유도)",
      "take": "내 작품에 가져올 것 한 가지",
      "change": "내 방식으로 바꿀 것 한 가지"
    }
  ],
  "patterns": {
    "title": "이 장르 제목 패턴 한 줄",
    "length": "잘 되는 길이대 한 줄",
    "story": "반복되는 이야기 구조 한 줄",
    "rhythm": "추정 편집 리듬 한 줄"
  }
}"""

SUMMARY_SCHEMA = """{
  "common": [
    {"name": "제목 패턴", "detail": "한두 문장"},
    {"name": "길이대", "detail": "한두 문장"},
    {"name": "이야기 구조", "detail": "한두 문장"}
  ],
  "rising": ["지난 7일 대비 새로 뜨는 형식 (근거 포함, 1~3개)"],
  "cooling": ["식는 형식 (근거 포함, 0~3개). 비교할 과거 자료가 없으면 빈 목록"],
  "ideas": [
    {"title": "회차 제목 예시", "background": "배경", "hook": "첫 3초 훅", "structure": "구조 (예: 무시 → 반전 → 사이다)", "length": "추천 길이",
     "cast": [{"role": "역할", "who": "인물 설정 한 줄"}], "flow": ["흐름 1단계", "2단계", "3단계"], "ending": "결말 한 줄"}
  ],
  "popular": ["숏드라마가 아닌 인기 숏폼에서 지금 잘 되는 형식 (근거 포함, 1~3개). 인기 숏폼 자료가 없으면 빈 목록"]
}"""

WATCH_SCHEMA = """{
  "format": "영상 종류 한 단어 (숏드라마/상황극/동물/애니메이션/썰·사연/정보/챌린지/기타)",
  "audio": "소리 방식 (인물 대사 / 내레이션 / 자막만 / 음악만)",
  "hook": "첫 3초에 시선을 잡는 방식 한 줄",
  "cast": [{"role": "역할", "kind": "사람/동물/캐릭터/사물 중 하나", "who": "설정 한 줄", "look": "외형 한 줄 (그림으로 다시 그릴 수 있게)"}],
  "script": [{"t": "0~3초", "scene": "화면에 보이는 것 한 줄", "line": "그 장면의 말·내레이션·자막 요지 (없으면 빈 문자열)"}],
  "flow": ["이야기 흐름 1단계", "2단계", "3단계 (결말 직전까지 3~5단계)"],
  "ending": "결말 한 줄",
  "structure": "이야기 구조 (예: 무시 → 반전 → 사이다)"
}"""


# ---------------- 제미나이 호출 ----------------

class Gemini:
    def __init__(self, cfg, mock=False):
        self.cfg = cfg["gemini"]
        self.mock = mock
        self.requests = 0
        self.last = 0.0
        self.model_used = self.cfg["model"]
        self.dead = set()   # (키 번호, 모델): 오늘 한도가 찬 조합은 다시 쓰지 않음
        self.clients = []
        if not mock:
            from google import genai
            keys = gemini_keys()
            if not keys:
                raise RuntimeError("GEMINI_API_KEY가 없습니다 (깃허브 Secrets 확인)")
            self.clients = [genai.Client(api_key=k) for k in keys]
            print(f"[분석] 제미나이 키 {len(keys)}개 사용 (한도가 차면 다음 키로 넘어감)")
        self.client = self.clients[0] if self.clients else None

    def _wait(self):
        gap = self.cfg["wait_between_requests_sec"] - (time.time() - self.last)
        if gap > 0 and self.requests:
            time.sleep(gap)
        self.last = time.time()

    def _call(self, model, parts, can_switch=False):
        from google.genai import errors, types
        tries = 0
        while True:
            self._wait()
            self.requests += 1
            try:
                res = self.client.models.generate_content(
                    model=model, contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=self.cfg["temperature"]))
                return res.text or ""
            except errors.APIError as e:
                retryable = e.code == 429 or (e.code or 0) >= 500
                if e.code == 429 and can_switch:
                    raise   # 다른 키가 남아 있으면 기다리지 않고 바로 다음 키로
                if retryable and tries < self.cfg["max_retries"]:
                    tries += 1
                    wait = self.cfg["retry_wait_sec"] * tries
                    print(f"[분석] {e.code} 오류 → {wait}초 뒤 다시 시도 ({tries}/{self.cfg['max_retries']})")
                    time.sleep(wait)
                    continue
                raise

    def ask_json(self, parts, mock_fn):
        """JSON으로 받기. 형식이 깨지면 한 번 더 요청. 기본 모델 실패 시 보조 모델 1회."""
        if self.mock:
            self.requests += 1
            return mock_fn()
        models = [self.cfg["model"]] + ([self.cfg["fallback_model"]] if self.cfg.get("fallback_model") else [])
        last_err = RuntimeError("모든 키·모델이 오늘 한도 초과")
        for model in models:
            for ki, client in enumerate(self.clients):
                if (ki, model) in self.dead:
                    continue
                self.client = client
                left = [k for k in range(len(self.clients)) if k != ki and (k, model) not in self.dead]
                try:
                    for attempt in range(2):
                        text = self._call(model, parts if attempt == 0 else parts + [
                            "직전 답이 올바른 JSON 객체가 아니었습니다. 설명 없이 지정한 형식의 JSON 객체만 다시 보내 주세요."], can_switch=bool(left))
                        data = parse_json(text)
                        if isinstance(data, list):   # 객체 대신 목록으로 오면 안의 객체를 꺼낸다
                            data = next((x for x in data if isinstance(x, dict)), None)
                        if isinstance(data, dict):
                            self.model_used = model if ki == 0 else f"{model} (키 {ki + 1})"
                            return data
                        print("[분석] JSON 형식이 깨져서 한 번 더 요청합니다")
                    raise ValueError("JSON 형식 오류가 2번 연속 발생")
                except Exception as e:
                    last_err = e
                    if getattr(e, "code", None) == 429:
                        self.dead.add((ki, model))
                        print(f"[분석] 키 {ki + 1} · {model} 한도 초과 → 다음 키로")
                    else:
                        print(f"[분석] 키 {ki + 1} · 모델 {model} 실패: {scrub(e)[:200]}")
                        break   # 한도 문제가 아니면 같은 모델로 다른 키를 써도 같으므로 다음 모델로
        raise last_err


def gemini_keys():
    """GEMINI_API_KEY(쉼표로 여러 개 가능) + GEMINI_API_KEY_2 ~ GEMINI_API_KEY_9"""
    raw = [os.environ.get("GEMINI_API_KEY", "")] + [os.environ.get(f"GEMINI_API_KEY_{i}", "") for i in range(2, 10)]
    keys = [k.strip() for r in raw for k in re.split(r"[,\s]+", r or "") if k.strip()]
    return list(dict.fromkeys(keys))


def parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                return None
    return None


def fetch_thumb(url):
    """유튜브가 API로 알려 준 썸네일 주소만 받는다 (저장하지 않음)."""
    if not url.startswith("https://i.ytimg.com/"):
        return None
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200 and len(r.content) < 2_000_000:
            return r.content
    except Exception:
        pass
    return None


# ---------------- 입력 정리 ----------------

def video_brief(v):
    return {
        "video_id": v["video_id"], "title": v["title"], "channel": v["channel"],
        "published": v["published_at"][:10], "duration_sec": v["duration_sec"],
        "views": v["views"], "likes": v["likes"], "comments": v["comments"],
        "subscribers": v.get("subscribers"), "views_per_subscriber": v.get("view_sub_ratio"),
        "daily_growth": v.get("velocity"), "description_head": v["description_head"][:150],
        "hashtags": v["hashtags"][:8],
        "top_comments": [c["text"][:120] for c in v.get("top_comments", [])[:10]],
    }


def kind_of(v):
    return v.get("kind") or "drama"


def yt_watch_url(v):
    return f"https://www.youtube.com/watch?v={v['video_id']}"


def clean_cast(cast):
    """cast 를 [{role, kind, who, look}] 로 고르게 맞춘다 (없으면 빈 목록)."""
    out = []
    for c in cast if isinstance(cast, list) else []:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "사람").strip()
        kind = next((k for k in ("동물", "캐릭터", "사물", "사람") if k in kind), "사람")
        out.append({"role": str(c.get("role") or "인물"), "kind": kind,
                    "who": str(c.get("who") or c.get("desc") or ""), "look": str(c.get("look") or "")})
    return out[:4]


def clean_script(script):
    out = []
    for x in script if isinstance(script, list) else []:
        if isinstance(x, dict) and (x.get("scene") or x.get("line")):
            out.append({"t": str(x.get("t") or ""), "scene": str(x.get("scene") or ""), "line": str(x.get("line") or "")})
    return out[:10]


def tidy_video(a):
    """영상 한 편의 분석을 앱이 읽기 좋게 고른다."""
    if not isinstance(a, dict):
        return a
    a["cast"] = clean_cast(a.get("cast"))
    a["script"] = clean_script(a.get("script"))
    if not isinstance(a.get("flow"), list):
        a["flow"] = [str(a["flow"])] if a.get("flow") else []
    return a


def watch_targets(vids, wc):
    """직접 볼 영상 고르기: 종류(kinds) 순서대로, 장르마다 상위 per_genre 개, 모두 합쳐 max_total 개."""
    out = []
    for kind in wc.get("kinds") or ["popular"]:
        for rank in range(1, int(wc.get("per_genre", 1)) + 1):
            for v in vids:
                if kind_of(v) == kind and v.get("rank") == rank and v["duration_sec"] <= int(wc.get("max_duration_sec", 90)):
                    out.append(v)
    return out[:int(wc.get("max_total", 0))]


def past_patterns(today, days=7):
    """지난 7일 analysis.json에서 장르별 패턴·공통 구조를 모은다."""
    out = []
    base = datetime.strptime(today, "%Y-%m-%d")
    for i in range(1, days + 1):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        p = DATA / d / "analysis.json"
        if p.exists():
            try:
                a = read_json(p)
                if a.get("sample"):
                    continue
                out.append({"date": d, "patterns": {g: r.get("patterns") for g, r in a.get("genres", {}).items()},
                            "common": a.get("summary", {}).get("common")})
            except Exception:
                pass
    return out


def length_bands(videos):
    bands = {"30초 이하": 0, "31~60초": 0, "61~120초": 0, "121~180초": 0}
    for v in videos:
        s = v["duration_sec"]
        k = "30초 이하" if s <= 30 else "31~60초" if s <= 60 else "61~120초" if s <= 120 else "121~180초"
        bands[k] += 1
    return bands


# ---------------- 가짜 응답 (시험용) ----------------

def mock_genre(genre, vids):
    hooks = ["반전 예고형", "대사 인용형", "질문형", "숫자·회차형"]
    return {
        "videos": [{
            "video_id": v["video_id"], "hook": f"{hooks[i % 4]} (샘플)",
            "title_ko": "(샘플) 한국어로 옮긴 제목" if v.get("market") == "global" else "",
            "structure": "무시당함 → 정체 공개 → 사이다 응징 (추정·샘플)",
            "format": mock_format(genre), "audio": "내레이션 (샘플)" if kind_of(v) == "popular" else "인물 대사 (샘플)",
            "cast": mock_cast(genre),
            "script": [{"t": "0~3초", "scene": "주인공이 화면 가운데로 들어온다 (샘플)", "line": "오늘 무슨 일이 있었는지 한 줄로 알린다 (샘플)"},
                       {"t": "3~8초", "scene": "상대가 끼어들어 방해한다 (샘플)", "line": "상대가 비웃는 말을 한다 (샘플)"},
                       {"t": "8~14초", "scene": "주인공이 뜻밖의 행동을 한다 (샘플)", "line": ""},
                       {"t": "14~20초", "scene": "상황이 뒤집히고 모두 놀란다 (샘플)", "line": "마지막 한마디로 끝낸다 (샘플)"}],
            "flow": ["입주민이 배달원을 공개적으로 모욕 (샘플)", "주민들이 구경하며 비웃음 (샘플)", "관리인이 배달원을 '회장님'이라 부름 (샘플)"],
            "ending": "입주민이 관리 규정 위반으로 쫓겨남 (샘플)",
            "thumbnail": "놀란 표정 클로즈업 + 노란 큰 글자 (샘플)",
            "reaction": "반전 순간과 다음편 요청에 반응 (샘플)",
            "series": "회차 번호 있음, 다음편 유도 (샘플)" if "화" in v["title"] or "EP" in v["title"] else "단편 (샘플)",
            "take": "첫 3초에 결말 일부를 보여주기 (샘플)", "change": "배경을 배달 현장으로 바꾸기 (샘플)",
        } for i, v in enumerate(vids)],
        "patterns": {"title": f"{genre}: 결과 먼저 말하는 제목 (샘플)", "length": "50~70초 (샘플)",
                     "story": "무시 → 반전 → 사이다 (샘플)", "rhythm": "2~3초마다 컷 (샘플)"},
    }


def mock_format(genre):
    return next((f for k, f in (("동물", "동물"), ("애니", "애니메이션"), ("상황극", "상황극"), ("썰", "썰·사연")) if k in genre),
                "숏드라마" if "인기" not in genre else "기타") + " (샘플)"


def mock_cast(genre):
    if "동물" in genre:
        return [{"role": "주인공", "kind": "동물", "who": "간식을 숨겨 두고 시치미 떼는 강아지 (샘플)", "look": "작은 흰색 말티즈, 동그란 검은 눈, 빨간 목줄 (샘플)"},
                {"role": "상대", "kind": "동물", "who": "간식을 노리는 고양이 (샘플)", "look": "회색 줄무늬 고양이, 노란 눈, 통통한 몸 (샘플)"}]
    if "애니" in genre:
        return [{"role": "주인공", "kind": "캐릭터", "who": "실수투성이 꼬마 로봇 (샘플)", "look": "둥근 머리의 파란 3D 로봇, 큰 노란 눈, 짧은 팔다리 (샘플)"}]
    if "종합" in genre:
        return []
    return [{"role": "주인공", "kind": "사람", "who": "무시당하는 30대 배달원, 사실 건물주 (샘플)", "look": "30대 남성, 짧은 검은 머리, 낡은 배달 조끼, 차분한 눈빛 (샘플)"},
            {"role": "악역", "kind": "사람", "who": "갑질하는 고급 아파트 입주민 (샘플)", "look": "50대 여성, 화려한 정장과 진주 목걸이, 날카로운 인상 (샘플)"}]


def mock_watch(v):
    r = mock_genre(v["genre"], [v])["videos"][0]
    return {k: r[k] for k in ("format", "audio", "hook", "cast", "script", "flow", "ending", "structure")}


def mock_summary(bg):
    return {
        "common": [{"name": "제목 패턴", "detail": "결과·반전을 제목에 먼저 공개 (샘플)"},
                   {"name": "길이대", "detail": "50~70초가 가장 많음 (샘플)"},
                   {"name": "이야기 구조", "detail": "무시 → 정체 공개 → 응징 (샘플)"}],
        "rising": ["회차 번호를 제목 앞에 붙이는 형식 (샘플)"], "cooling": [],
        "ideas": [{"title": f"{b}인 줄 알았던 남자의 정체 (샘플)", "background": b, "hook": "무시하는 한마디로 시작",
                   "structure": "무시 → 반전 → 사이다", "length": "60초",
                   "cast": [{"role": "주인공", "who": f"무시당하는 {b} (샘플)"}, {"role": "악역", "who": "겉모습으로 사람을 판단하는 갑 (샘플)"}],
                   "flow": ["공개적인 무시 (샘플)", "숨은 정체를 암시하는 단서 (샘플)", "정체 공개 (샘플)"], "ending": "악역이 사과하고 대가를 치름 (샘플)"} for b in bg[:3]],
        "popular": ["동물이 사람처럼 속마음을 말하는 내레이션 형식 (샘플)"],
    }


# ---------------- 결과 문서 ----------------

def render_md(today, raw, genres, summary, failures, model, requests_count):
    vids = raw["videos"]
    L = [f"# 숏폼 유행 형식 분석 — {today}", ""]
    if raw.get("sample"):
        L += ["> ⚠️ 샘플 데이터로 만든 시험용 결과입니다.", ""]
    if failures:
        L += ["> ⚠️ 분석 일부 실패: " + " / ".join(failures), ""]
    L += [f"- 수집 영상: {len(vids)}개 · 제미나이 모델: `{model}` · 요청 {requests_count}회",
          f"- 유튜브 할당량 사용: {raw.get('quota', {}).get('used_units', '-')}단위 / 10,000", ""]

    L += ["## 오늘 장르별 상위 3개", ""]
    for g in dict.fromkeys(v["genre"] for v in vids):
        L.append(f"### {g}")
        for v in [x for x in vids if x["genre"] == g][:3]:
            ko = next((x.get("title_ko") for x in (genres.get(g) or {}).get("videos", []) if x.get("video_id") == v["video_id"] and x.get("title_ko")), "")
            L.append(f"{v['rank']}. [{v['title']}]({v['url']}) — 조회 {fmt_num(v['views'])}, {fmt_dur(v['duration_sec'])}" + (f"\n   - 한국어 제목: {ko}" if ko else ""))
        L.append("")

    if summary:
        L += ["## 반복해서 나타나는 공통 구조", ""]
        for c in summary.get("common", []):
            L.append(f"- **{c.get('name')}**: {c.get('detail')}")
        L += ["", "## 지난 7일 대비", "", "**새로 뜨는 형식**"]
        L += [f"- {x}" for x in summary.get("rising", [])] or ["- (자료 부족)"]
        L += ["", "**식는 형식**"]
        L += [f"- {x}" for x in summary.get("cooling", [])] or ["- (아직 비교할 과거 자료가 부족)"]
        if summary.get("popular"):
            L += ["", "**인기 숏폼에서 잘 되는 형식**"]
            L += [f"- {x}" for x in summary["popular"]]
        L += ["", "## 내일 바로 써먹을 회차 아이디어", ""]
        for i, x in enumerate(summary.get("ideas", []), 1):
            L.append(f"{i}. **{x.get('title')}** ({x.get('background')}, {x.get('length')})")
            L.append(f"   - 훅: {x.get('hook')} / 구조: {x.get('structure')}")
            if x.get("cast"):
                L.append("   - 인물: " + " / ".join(f"{c.get('role')}: {c.get('who')}" for c in x["cast"]))
            if x.get("flow"):
                L.append("   - 흐름: " + " → ".join(x["flow"]) + (f" → 결말: {x.get('ending')}" if x.get("ending") else ""))
        L.append("")

    L += ["## 장르별 형식 해부", ""]
    for g in dict.fromkeys(v["genre"] for v in vids):
        res = genres.get(g)
        L.append(f"### {g}")
        if not res:
            L += ["(분석 실패 — 수집 데이터만 표시)", ""]
        else:
            p = res.get("patterns", {})
            L += [f"- 제목 패턴: {p.get('title')}", f"- 길이대: {p.get('length')}",
                  f"- 이야기 구조: {p.get('story')}", f"- 편집 리듬: {p.get('rhythm')}", ""]
        by_id = {x.get("video_id"): x for x in (res or {}).get("videos", [])}
        for v in [x for x in vids if x["genre"] == g]:
            a = by_id.get(v["video_id"], {})
            ratio = f"{v['view_sub_ratio']}배" if v.get("view_sub_ratio") else "구독자 비공개"
            L += ["```", "[유행 형식 분석]",
                  f"{v['url']} / 조회 {fmt_num(v['views'])} / {v['published_at'][:10]} / {fmt_dur(v['duration_sec'])} / 구독자 대비 {ratio}",
                  f"하루 상승: +{fmt_num(v.get('velocity', 0))} ({v.get('velocity_type', '-')})",
                  f"제목 훅 방식: {a.get('hook', '-')}",
                  f"추정 이야기 구조: {a.get('structure', '-')}",
                  f"종류: {a.get('format', '-')} / 소리: {a.get('audio', '-')}" + (" / 영상을 직접 보고 분석" if a.get("watched") else ""),
                  "등장인물: " + (" / ".join(f"{c.get('role')}({c.get('kind', '사람')}): {c.get('who')}" + (f" [외형: {c.get('look')}]" if c.get("look") else "")
                                         for c in a.get("cast", [])) or "없음"),
                  *(["장면별 대본:"] + [f"  {x.get('t', '')} {x.get('scene', '')}" + (f" — {x.get('line')}" if x.get("line") else "") for x in a["script"]] if a.get("script") else []),
                  "흐름: " + (" → ".join(a.get("flow", [])) or "-"),
                  f"결말: {a.get('ending', '-')}",
                  f"썸네일 특징: {a.get('thumbnail', '-')}",
                  f"댓글에서 사람들이 반응한 지점: {a.get('reaction', '-')}",
                  f"시리즈 여부: {a.get('series', '-')}",
                  f"→ 내 작품에 가져올 것 한 가지: {a.get('take', '-')}",
                  f"→ 내 방식으로 바꿀 것 한 가지: {a.get('change', '-')}", "```", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="키 없이 가짜 응답으로 시험")
    args = ap.parse_args()
    cfg = load_config()
    today = today_kst()
    d = day_dir(today)
    raw = read_json(d / "raw.json")
    vids = raw["videos"]
    failures, genres, summary = [], {}, None

    try:
        gm = Gemini(cfg, mock=args.mock)
    except Exception as e:
        gm = None
        failures.append(f"제미나이 준비 실패: {scrub(e)}")

    if gm and vids:
        n_thumb = cfg["gemini"]["thumbnails_per_genre"]
        for g in dict.fromkeys(v["genre"] for v in vids):
            gv = [v for v in vids if v["genre"] == g]
            parts = []
            from_img = []
            if not args.mock:
                from google.genai import types
                for v in gv[:n_thumb]:
                    img = fetch_thumb(v["thumbnail"])
                    if img:
                        parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
                        from_img.append(v["video_id"])
            overseas = any(v.get("market") == "global" for v in gv)
            local = ("이 장르는 해외(영어권) 인기 영상이다. 모든 답은 한국어로 쓰고, title_ko에 제목을 자연스러운 한국어로 번역한다. "
                     "cast·flow·ending은 한국 시청자가 바로 찍을 수 있게 한국식 배경·직업·인물로 바꿔 적되 흐름(단계·반전 위치)은 그대로 둔다.\n") if overseas else ""
            popular = kind_of(gv[0]) == "popular"
            who = ("너는 숏폼(쇼츠·릴스) 형식 분석가다. 이 묶음은 숏드라마가 아닌 일반 인기 숏폼이다. "
                   "드라마 공식에 끼워 맞추지 말고 영상 종류(상황극·동물·애니메이션·썰·정보 등)에 맞게 적는다. "
                   "동물·캐릭터가 주인공이면 cast 에 그대로 적고, 등장인물이 없으면 cast 는 빈 목록으로 둔다.") if popular else "너는 숏드라마 형식 분석가다."
            prompt = (f"{who} 장르: {g}\n{local}"
                      f"아래는 최근 7일 유튜브 쇼츠 인기 영상 {len(gv)}개의 공개 정보다.\n"
                      f"함께 붙인 썸네일 이미지 {len(from_img)}장은 순서대로 이 영상들의 것이다: {from_img}\n"
                      f"모든 영상에 대해 템플릿을 채워라.\n{RULES}\n\n"
                      f"영상 정보(JSON):\n{json.dumps([video_brief(v) for v in gv], ensure_ascii=False)}\n\n"
                      f"답 형식(JSON):\n{GENRE_SCHEMA}")
            try:
                res = gm.ask_json(parts + [prompt], lambda: mock_genre(g, gv))
                if not isinstance(res.get("videos"), list):
                    res["videos"] = [res] if res.get("video_id") else []
                res.setdefault("patterns", {})
                res["videos"] = [tidy_video(x) for x in res["videos"] if isinstance(x, dict)]
                genres[g] = res
                print(f"[분석] {g} 완료")
            except Exception as e:
                failures.append(f"{g} 분석 실패({scrub(e)[:80]})")

        if genres:
            bg = cfg.get("my_background", [])
            ctx = {
                "today_genre_patterns": {g: r.get("patterns") for g, r in genres.items()},
                "today_hooks": {g: [x.get("hook") for x in r.get("videos", [])] for g, r in genres.items()},
                "today_length_bands": length_bands(vids),
                "fast_rising": [{"genre": v["genre"], "title": v["title"], "daily_growth": v.get("velocity"),
                                 "duration_sec": v["duration_sec"]}
                                for v in sorted(vids, key=lambda v: v.get("velocity", 0), reverse=True)[:8]],
                "past_7_days": past_patterns(today),
            }
            prompt = ("너는 숏드라마 채널 기획 도우미다. 오늘 장르별 분석 결과와 지난 7일 기록을 비교해 요약하라.\n"
                      "- common: 여러 장르에 반복되는 공통 구조 정확히 3가지 (제목 패턴, 길이대, 이야기 구조)\n"
                      "- popular: 이름이 '인기' 또는 '해외 인기'로 시작하는 장르(숏드라마가 아닌 일반 인기 숏폼)에서 지금 잘 되는 형식 1~3개\n"
                      "- rising/cooling: past_7_days와 비교. 과거 자료가 없으면 오늘 상승 속도(fast_rising) 기준으로 rising만 쓰고 cooling은 빈 목록\n"
                      f"- ideas: 내일 바로 찍을 회차 아이디어 정확히 3개. 배경은 {bg} 중에서 하나씩 골라 바꿔라. "
                      "남의 영상 줄거리를 그대로 옮기지 말고 형식만 빌려 새 이야기로.\n"
                      f"{RULES}\n\n자료(JSON):\n{json.dumps(ctx, ensure_ascii=False)}\n\n답 형식(JSON):\n{SUMMARY_SCHEMA}")
            try:
                summary = gm.ask_json([prompt], lambda: mock_summary(bg))
                print("[분석] 전체 요약 완료")
            except Exception as e:
                failures.append(f"전체 요약 실패({scrub(e)[:80]})")

    # 영상 직접 보기: 고른 영상을 제미나이가 실제로 보고 장면별 대본·등장인물 외형을 다시 적는다.
    # 실패해도 위의 추정 분석이 남으므로 멈추지 않는다.
    wc = cfg.get("watch") or {}
    watched, misses = 0, 0
    if gm and int(wc.get("max_total", 0)) > 0:
        for v in watch_targets(vids, wc):
            if misses >= 2:      # 연속으로 안 되면 한도·권한 문제이므로 남은 요청을 아낀다
                break
            parts = ["(가짜 응답)"] if args.mock else None
            if not args.mock:
                from google.genai import types
                parts = [types.Part(file_data=types.FileData(file_uri=yt_watch_url(v)))]
            overseas = v.get("market") == "global"
            prompt = ("너는 숏폼 영상 분석가다. 붙인 유튜브 영상을 처음부터 끝까지 직접 보고 답 형식을 채워라.\n"
                      "- 직접 본 내용만 적는다. 추정 표시는 붙이지 않는다.\n"
                      "- 대사·내레이션·자막은 그대로 받아쓰지 말고 요지를 내 말로 적는다. 실존 인물 이름, 기존 작품 캐릭터 이름·상표는 쓰지 않고 생김새로만 적는다.\n"
                      "- cast 의 kind 는 사람/동물/캐릭터/사물 중 하나. 등장인물이 없으면 cast 는 빈 목록 [].\n"
                      "- look 은 그림으로 다시 그릴 수 있게 외형만 (사람: 나이대·성별·머리·옷·인상 / 동물: 종·털색·크기 / 캐릭터: 그림체·색·특징).\n"
                      "- script 는 장면 순서대로 4~8줄, t 는 그 장면의 시간 구간.\n"
                      + ("- 해외 영상이다. 모든 답은 한국어로 쓴다.\n" if overseas else "- 모든 답은 쉬운 한국어로 쓴다.\n")
                      + f"영상 길이: {v['duration_sec']}초. 반드시 지정한 JSON 형식으로만 답한다.\n\n답 형식(JSON):\n{WATCH_SCHEMA}")
            try:
                res = tidy_video(gm.ask_json(parts + [prompt], lambda: mock_watch(v)))
                if not res.get("script"):
                    raise ValueError("대본이 비어 있음")
                g = genres.setdefault(v["genre"], {"videos": [], "patterns": {}})
                cur = next((x for x in g["videos"] if x.get("video_id") == v["video_id"]), None)
                if cur is None:
                    cur = {"video_id": v["video_id"]}
                    g["videos"].append(cur)
                cur.update({k: res[k] for k in ("format", "audio", "hook", "cast", "script", "flow", "ending", "structure") if res.get(k) not in (None, "")})
                cur["cast"] = res["cast"]          # 직접 봤으면 "등장인물 없음"도 그대로 믿는다
                cur["watched"] = True
                watched, misses = watched + 1, 0
                print(f"[분석] 영상 직접 보기 완료: {v['video_id']} ({v['genre']})")
            except Exception as e:
                misses += 1
                print(f"[분석] 영상 직접 보기 건너뜀 {v['video_id']}: {scrub(e)[:160]}")
        if watched:
            print(f"[분석] 직접 본 영상 {watched}개")

    model = gm.model_used if gm else "-"
    reqs = gm.requests if gm else 0
    if args.mock:
        model += " (가짜 응답)"
    md = render_md(today, raw, genres, summary, failures, model, reqs)
    (d / "analysis.md").write_text(md, encoding="utf-8")
    write_json(d / "analysis.json", {"date": today, "sample": bool(raw.get("sample")), "model": model, "requests": reqs,
                                     "genres": genres, "summary": summary, "failures": failures,
                                     "genre_info": {g: {"market": next((v.get("market", "kr") for v in vids if v["genre"] == g), "kr"),
                                                        "kind": next((kind_of(v) for v in vids if v["genre"] == g), "drama")}
                                                    for g in dict.fromkeys(v["genre"] for v in vids)},
                                     "watched": watched})
    print(f"[분석] 제미나이 요청 {reqs}회 · 모델 {model}")
    ok = bool(summary) and not failures
    write_status(today, "analyze", ok, " / ".join(failures) if failures else f"요청 {reqs}회")
    if failures:
        print("[분석 일부 실패] " + " / ".join(failures), file=sys.stderr)
    # 분석이 실패해도 0으로 끝내서 전송 단계가 이어지게 한다 (실패는 status.json으로 전달)


if __name__ == "__main__":
    main()
