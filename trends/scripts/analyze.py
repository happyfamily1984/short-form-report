"""② 형식 분석 — 제미나이 무료 키 (google-genai).

사용법:
  python trends/scripts/analyze.py          # 실제 분석 (GEMINI_API_KEY 필요)
  python trends/scripts/analyze.py --mock   # 키 없이 가짜 응답으로 흐름 시험

요청 수: 장르 수(교육·과학·다큐 x 한국·해외 = 6) + 전체 요약(1) + 영상 직접 보기(watch.max_total, 초 단위 분석 포함) = 하루 약 17회
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
- 분야는 교육·과학·다큐멘터리 정보형 숏폼이다. 영상의 "형식"(주제 고르는 법, 훅, 장면 구성, 내레이션, 화면 방식, 궁금증을 끄는 장치)을 분석한다.
- 제목·설명·댓글·대사를 그대로 옮겨 적지 않는다. 반드시 내 말로 요약한다 (따옴표 인용 금지).
- 영상 본편을 직접 본 것이 아니므로, 제목·설명·댓글·길이·썸네일로 추정한 내용은 끝에 "(추정)"을 붙인다.
- 모든 답은 쉬운 한국어로, 항목마다 1~2문장으로 짧게.
- script 는 장면 순서대로 4~8줄. 줄마다 화면에 보이는 것(scene)과 그 장면의 내레이션·자막 요지(line)를 내 말로 적는다. 다른 사람이 주제만 바꿔 같은 구성의 새 영상을 만들 수 있게 구체적으로.
- facts 는 영상이 전하는 핵심 사실 2~4개를 요약한다. 영상의 주장일 뿐이므로 맞는지 판단하지 않는다.
- cast 는 화면에 계속 나오는 주체(진행자·동물·캐릭터·사물)만 적는다. 자료화면·손·풍경만 나오면 빈 목록 []. kind 는 사람/동물/캐릭터/사물 중 하나, look 은 그림으로 다시 그릴 수 있는 외형 한 줄. 실존 인물 이름, 기존 작품 캐릭터 이름·상표는 쓰지 않는다.
- 반드시 지정한 JSON 형식으로만 답한다."""

GENRE_SCHEMA = """{
  "videos": [
    {
      "video_id": "영상 ID 그대로",
      "title_ko": "해외 영상만: 제목을 자연스러운 한국어로 번역 (한국 영상은 빈 문자열)",
      "topic": "다루는 주제 한 줄 (예: 번개는 왜 지그재그로 칠까)",
      "format": "구성 방식 한 단어 (질문 풀이/놀라운 사실/원리 해부/비교/순위/역사 이야기/실험/기타)",
      "visual": "화면 방식 (실사 자료화면/3D 애니메이션/모션그래픽/일러스트/진행자 출연/자막 위주)",
      "audio": "소리 방식 (내레이션 / 진행자 말 / 자막만 / 음악만)",
      "hook": "첫 3초에 시선을 잡는 방식 (예: 상식을 뒤집는 질문, 충격적인 숫자, 결과 먼저 보여 주기)",
      "structure": "구성 흐름 (예: 질문 → 흔한 오해 → 진짜 원리 → 반전 사실 → 한 줄 결론)",
      "loops": "끝까지 보게 만드는 장치 한 줄 (예: 답을 마지막까지 미룸, 중간에 '그런데 더 이상한 건')",
      "cast": [{"role": "역할 (진행자/주인공/설명 캐릭터 등)", "kind": "사람/동물/캐릭터/사물", "who": "설정 한 줄", "look": "외형 한 줄"}],
      "script": [{"t": "0~3초", "scene": "화면에 보이는 것 한 줄", "line": "그 장면의 내레이션·자막 요지 (없으면 빈 문자열)"}],
      "facts": ["영상이 전하는 핵심 사실 요약"],
      "flow": ["흐름 1단계 한 줄", "2단계", "3단계 (마무리 직전까지 3~5단계)"],
      "ending": "마무리 한 줄 (마지막 한 방이나 결론)",
      "thumbnail": "썸네일·첫 화면 특징 (글자, 색, 피사체). 이미지를 못 받은 영상은 제목 기준 추정",
      "reaction": "댓글에서 사람들이 반응한 지점 (요약)",
      "series": "시리즈 여부 (번호, 재생목록, 다음 편 유도)",
      "take": "내 영상에 가져올 것 한 가지",
      "change": "내 방식으로 더 좋게 바꿀 것 한 가지"
    }
  ],
  "patterns": {
    "title": "이 분야 제목 패턴 한 줄",
    "length": "잘 되는 길이대 한 줄",
    "story": "반복되는 구성 한 줄",
    "rhythm": "장면 전환·편집 리듬 한 줄"
  }
}"""

SUMMARY_SCHEMA = """{
  "common": [
    {"name": "제목 패턴", "detail": "한두 문장"},
    {"name": "길이대", "detail": "한두 문장"},
    {"name": "구성", "detail": "한두 문장"}
  ],
  "rising": ["지난 7일 대비 새로 뜨는 주제나 형식 (근거 포함, 1~3개)"],
  "cooling": ["식는 주제나 형식 (근거 포함, 0~3개). 비교할 과거 자료가 없으면 빈 목록"],
  "ideas": [
    {"field": "교육/과학/다큐 중 하나", "title": "영상 제목 예시", "topic": "주제 한 줄", "hook": "첫 3초 훅", "structure": "구성 흐름", "length": "추천 길이",
     "why": "지금 이 주제가 될 이유 한 줄", "flow": ["흐름 1단계", "2단계", "3단계"], "ending": "마무리 한 줄"}
  ]
}"""

WATCH_SCHEMA = """{
  "topic": "다루는 주제 한 줄",
  "format": "구성 방식 한 단어 (질문 풀이/놀라운 사실/원리 해부/비교/순위/역사 이야기/실험/기타)",
  "visual": "화면 방식 (실사 자료화면/3D 애니메이션/모션그래픽/일러스트/진행자 출연/자막 위주)",
  "audio": "소리 방식 (내레이션 / 진행자 말 / 자막만 / 음악만)",
  "hook": "첫 3초에 시선을 잡는 방식 한 줄",
  "structure": "구성 흐름",
  "loops": "끝까지 보게 만드는 장치 한 줄",
  "cast": [{"role": "역할", "kind": "사람/동물/캐릭터/사물", "who": "설정 한 줄", "look": "외형 한 줄"}],
  "script": [{"t": "0~3초", "scene": "화면에 보이는 것 한 줄", "line": "그 장면의 내레이션·자막 요지 (없으면 빈 문자열)"}],
  "facts": ["영상이 전하는 핵심 사실 요약"],
  "flow": ["흐름 1단계", "2단계", "3단계"],
  "ending": "마무리 한 줄",
  "timeline": [{"s": 0, "visual": "이 1초에 화면에 보이는 것 (피사체·구도·카메라 움직임·색감)", "text": "화면에 뜬 글자 요지 (없으면 빈 문자열)",
                "audio": "들리는 것 (내레이션 요지·효과음·음악 변화, 없으면 빈 문자열)", "cut": true,
                "prompt": "이 장면을 새로 만들 때 쓸 영어 이미지·영상 프롬프트 한 줄 (구도·피사체·조명·카메라 움직임)"}]
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
    return v.get("kind") or "edu"


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


def clean_timeline(tl, step):
    """초 단위 분석: 시간 순서로 정렬하고, 한 칸에 한 줄씩. cut 은 그 초에 장면이 바뀌었는지."""
    out = []
    for x in tl if isinstance(tl, list) else []:
        if not isinstance(x, dict) or not (x.get("visual") or x.get("prompt")):
            continue
        try:
            sec = max(0, int(round(float(x.get("s", 0)))))
        except Exception:
            continue
        out.append({"s": sec, "visual": str(x.get("visual") or "")[:160], "text": str(x.get("text") or "")[:80],
                    "audio": str(x.get("audio") or "")[:120], "cut": bool(x.get("cut")), "prompt": str(x.get("prompt") or "")[:300]})
    out.sort(key=lambda x: x["s"])
    if out:
        out[0]["cut"] = True
    return out[:int(120 / max(1, step))]


def cut_stats(tl):
    """장면 전환 횟수와 평균 컷 길이(초)."""
    if not tl:
        return None
    cuts = [x["s"] for x in tl if x.get("cut")] or [0]
    end = tl[-1]["s"] + 1
    return {"cuts": len(cuts), "avg_shot_sec": round(end / max(1, len(cuts)), 1)}


def tidy_video(a):
    """영상 한 편의 분석을 앱이 읽기 좋게 고른다."""
    if not isinstance(a, dict):
        return a
    a["cast"] = clean_cast(a.get("cast"))
    a["script"] = clean_script(a.get("script"))
    a["facts"] = [str(x) for x in a.get("facts") or [] if x][:5] if isinstance(a.get("facts"), list) else []
    if not isinstance(a.get("flow"), list):
        a["flow"] = [str(a["flow"])] if a.get("flow") else []
    return a


def watch_targets(vids, wc):
    """직접 볼 영상 고르기: 종류(kinds) 순서대로, 장르마다 상위 per_genre 개, 모두 합쳐 max_total 개."""
    out = []
    for kind in wc.get("kinds") or ["edu", "sci", "doc"]:
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

SAMPLE = {
    "edu": ("손가락 관절에서 왜 소리가 날까", "질문 풀이", "모션그래픽"),
    "sci": ("번개는 왜 지그재그로 칠까", "원리 해부", "3D 애니메이션"),
    "doc": ("단 하루 만에 사라진 도시", "역사 이야기", "실사 자료화면"),
}


def mock_genre(genre, vids):
    out = []
    for i, v in enumerate(vids):
        topic, fmt, visual = SAMPLE.get(kind_of(v), SAMPLE["edu"])
        out.append({
            "video_id": v["video_id"], "title_ko": "(샘플) 한국어로 옮긴 제목" if v.get("market") == "global" else "",
            "topic": f"{topic} (샘플)", "format": fmt, "visual": visual, "audio": "내레이션",
            "hook": ["상식을 뒤집는 질문", "충격적인 숫자", "결과 먼저 보여 주기"][i % 3] + " (샘플)",
            "structure": "질문 → 흔한 오해 → 진짜 원리 → 반전 사실 → 한 줄 결론 (샘플)",
            "loops": "답을 마지막 장면까지 미룬다 (샘플)",
            "cast": [{"role": "설명 캐릭터", "kind": "캐릭터", "who": "궁금한 걸 대신 묻는 꼬마 과학자 (샘플)", "look": "둥근 안경을 쓴 2D 꼬마, 흰 가운, 큰 눈 (샘플)"}] if kind_of(v) == "sci" else [],
            "script": [{"t": "0~3초", "scene": f"{topic} 장면을 크게 보여 준다 (샘플)", "line": "다들 이렇게 알고 있죠? 사실 틀렸습니다 (샘플)"},
                       {"t": "3~10초", "scene": "흔한 오해를 그림으로 보여 준다 (샘플)", "line": "흔히 믿는 설명 한 줄 (샘플)"},
                       {"t": "10~20초", "scene": "원리를 단면도·화살표로 보여 준다 (샘플)", "line": "진짜 원리 한 줄 (샘플)"},
                       {"t": "20~27초", "scene": "더 놀라운 사실로 확대 (샘플)", "line": "그런데 더 이상한 건 (샘플)"},
                       {"t": "27~32초", "scene": "처음 장면으로 돌아온다 (샘플)", "line": "한 줄 결론 (샘플)"}],
            "facts": ["영상이 전하는 핵심 사실 1 (샘플)", "핵심 사실 2 (샘플)"],
            "flow": ["상식 질문 (샘플)", "흔한 오해 (샘플)", "진짜 원리 (샘플)", "반전 사실 (샘플)"],
            "ending": "처음 질문에 한 줄로 답하며 끝 (샘플)",
            "thumbnail": "큰 물음표와 피사체 클로즈업, 노란 글자 (샘플)",
            "reaction": "몰랐다는 댓글과 다른 주제 요청 (샘플)",
            "series": "번호 붙은 시리즈 (샘플)" if i % 2 else "단편 (샘플)",
            "take": "답을 마지막까지 미루는 구성 (샘플)", "change": "화면 전환을 3초마다로 더 빠르게 (샘플)",
        })
    return {"videos": out, "patterns": {"title": f"{genre}: 질문형 제목 (샘플)", "length": "30~50초 (샘플)",
                                         "story": "질문 → 오해 → 원리 → 반전 (샘플)", "rhythm": "3~5초마다 화면 전환 (샘플)"}}


def mock_watch(v):
    r = mock_genre(v["genre"], [v])["videos"][0]
    out = {k: r[k] for k in ("topic", "format", "visual", "audio", "hook", "structure", "loops", "cast", "script", "facts", "flow", "ending")}
    out["timeline"] = [{"s": s, "visual": f"{s}초 화면 (샘플)", "text": "큰 질문 글자 (샘플)" if s < 3 else "", "audio": "내레이션 (샘플)",
                        "cut": s % 3 == 0, "prompt": f"vertical 9:16 shot {s} (sample)"} for s in range(min(int(v["duration_sec"]), 60))]
    return out


def mock_summary():
    return {
        "common": [{"name": "제목 패턴", "detail": "상식을 뒤집는 질문형 제목 (샘플)"},
                   {"name": "길이대", "detail": "30~50초가 가장 많음 (샘플)"},
                   {"name": "구성", "detail": "질문 → 오해 → 원리 → 반전 사실 (샘플)"}],
        "rising": ["우주·심해처럼 규모가 큰 주제 (샘플)"], "cooling": [],
        "ideas": [{"field": f, "title": f"{t} (샘플)", "topic": t, "hook": "상식을 뒤집는 질문", "structure": "질문 → 오해 → 원리 → 반전",
                   "length": "40초", "why": "비슷한 주제가 오늘 상승 중 (샘플)", "flow": ["질문", "오해", "원리"], "ending": "한 줄 결론"}
                  for f, t in (("교육", "왜 아침에 키가 더 클까"), ("과학", "달이 없으면 하루는 몇 시간일까"), ("다큐", "바다 밑에 가라앉은 도시"))],
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
        L += ["", "## 내일 만들 주제 아이디어", ""]
        for i, x in enumerate(summary.get("ideas", []), 1):
            L.append(f"{i}. **{x.get('title')}** ({x.get('field')}, {x.get('length')})")
            L.append(f"   - 주제: {x.get('topic')} / 훅: {x.get('hook')} / 구성: {x.get('structure')}")
            if x.get("why"):
                L.append(f"   - 지금 될 이유: {x.get('why')}")
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
                  f"추정 구성: {a.get('structure', '-')}",
                  f"주제: {a.get('topic', '-')}",
                  f"구성 방식: {a.get('format', '-')} / 화면: {a.get('visual', '-')} / 소리: {a.get('audio', '-')}" + (" / 영상을 직접 보고 분석" if a.get("watched") else ""),
                  f"끝까지 보게 하는 장치: {a.get('loops', '-')}",
                  "핵심 사실(영상의 주장): " + (" / ".join(a.get("facts", [])) or "-"),
                  "화면 주체: " + (" / ".join(f"{c.get('role')}({c.get('kind', '사람')}): {c.get('who')}" + (f" [외형: {c.get('look')}]" if c.get("look") else "")
                                         for c in a.get("cast", [])) or "없음"),
                  *([f"초 단위 분석: {len(a['timeline'])}칸 · 장면 전환 {a.get('cuts', {}).get('cuts', '-')}번 · 평균 {a.get('cuts', {}).get('avg_shot_sec', '-')}초마다"] if a.get("timeline") else []),
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
            local = ("이 묶음은 해외(영어권) 인기 영상이다. 모든 답은 한국어로 쓰고, title_ko에 제목을 자연스러운 한국어로 번역한다. "
                     "구성·장면은 그대로 두고, 한국 시청자에게 낯선 예시만 한국에서 통하는 예시로 바꿔 적는다.\n") if overseas else ""
            prompt = (f"너는 교육·과학·다큐멘터리 숏폼(쇼츠·릴스) 형식 분석가다. 분야: {g}\n{local}"
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
            ctx = {
                "today_genre_patterns": {g: r.get("patterns") for g, r in genres.items()},
                "today_hooks": {g: [x.get("hook") for x in r.get("videos", [])] for g, r in genres.items()},
                "today_length_bands": length_bands(vids),
                "fast_rising": [{"genre": v["genre"], "title": v["title"], "daily_growth": v.get("velocity"),
                                 "duration_sec": v["duration_sec"]}
                                for v in sorted(vids, key=lambda v: v.get("velocity", 0), reverse=True)[:8]],
                "past_7_days": past_patterns(today),
            }
            prompt = ("너는 교육·과학·다큐멘터리 숏폼 채널 기획 도우미다. 오늘 분야별 분석 결과와 지난 7일 기록을 비교해 요약하라.\n"
                      "- common: 여러 분야에 반복되는 공통 구조 정확히 3가지 (제목 패턴, 길이대, 구성)\n"
                      "- rising/cooling: past_7_days와 비교. 과거 자료가 없으면 오늘 상승 속도(fast_rising) 기준으로 rising만 쓰고 cooling은 빈 목록\n"
                      "- ideas: 내일 바로 만들 주제 아이디어 정확히 3개 (교육·과학·다큐 하나씩). 오늘 잘 된 구성을 빌리되 주제는 새로. "
                      "사실 확인이 쉬운 주제를 고른다.\n"
                      f"{RULES}\n\n자료(JSON):\n{json.dumps(ctx, ensure_ascii=False)}\n\n답 형식(JSON):\n{SUMMARY_SCHEMA}")
            try:
                summary = gm.ask_json([prompt], lambda: mock_summary())
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
            step = 1 if v["duration_sec"] <= 60 else 2
            prompt = ("너는 숏폼 영상 분석가다. 붙인 유튜브 영상을 처음부터 끝까지 직접 보고 답 형식을 채워라.\n"
                      "- 직접 본 내용만 적는다. 추정 표시는 붙이지 않는다.\n"
                      "- 대사·내레이션·자막은 그대로 받아쓰지 말고 요지를 내 말로 적는다. 실존 인물 이름, 기존 작품 캐릭터 이름·상표는 쓰지 않고 생김새로만 적는다.\n"
                      "- 교육·과학·다큐 정보형 숏폼으로 보고 분석한다. facts 는 영상이 전하는 핵심 사실 2~4개 요약.\n"
                      "- cast 는 화면에 계속 나오는 주체(진행자·동물·캐릭터·사물)만. kind 는 사람/동물/캐릭터/사물 중 하나. 자료화면·풍경뿐이면 빈 목록 [].\n"
                      "- look 은 그림으로 다시 그릴 수 있게 외형만 (사람: 나이대·성별·머리·옷·인상 / 동물: 종·털색·크기 / 캐릭터: 그림체·색·특징).\n"
                      "- script 는 장면 순서대로 4~8줄, t 는 그 장면의 시간 구간.\n"
                      f"- timeline 은 영상 처음부터 끝까지 {step}초마다 한 칸씩 빠짐없이 적는다 (s 는 그 칸이 시작하는 초). "
                      "cut 은 그 칸에서 장면이 바뀌면 true. prompt 는 그 장면을 새로 만들 때 쓸 영어 프롬프트로, 원본 영상의 글자·로고·실존 인물은 넣지 않는다.\n"
                      + ("- 해외 영상이다. 모든 답은 한국어로 쓴다.\n" if overseas else "- 모든 답은 쉬운 한국어로 쓴다.\n")
                      + f"영상 길이: {v['duration_sec']}초. 반드시 지정한 JSON 형식으로만 답한다.\n\n답 형식(JSON):\n{WATCH_SCHEMA}")
            try:
                res = tidy_video(gm.ask_json(parts + [prompt], lambda: mock_watch(v)))
                res["timeline"] = clean_timeline(res.get("timeline"), step)
                if not res.get("script"):
                    raise ValueError("대본이 비어 있음")
                g = genres.setdefault(v["genre"], {"videos": [], "patterns": {}})
                cur = next((x for x in g["videos"] if x.get("video_id") == v["video_id"]), None)
                if cur is None:
                    cur = {"video_id": v["video_id"]}
                    g["videos"].append(cur)
                cur.update({k: res[k] for k in ("topic", "format", "visual", "audio", "hook", "structure", "loops", "cast", "script", "facts", "flow", "ending") if res.get(k) not in (None, "", [])})
                cur["cast"] = res["cast"]          # 직접 봤으면 "등장인물 없음"도 그대로 믿는다
                if res["timeline"]:
                    cur["timeline"], cur["timeline_step"], cur["cuts"] = res["timeline"], step, cut_stats(res["timeline"])
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
                                                        "kind": next((kind_of(v) for v in vids if v["genre"] == g), "edu")}
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
