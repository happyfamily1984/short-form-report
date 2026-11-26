"""② 형식 분석 — 제미나이 무료 키 (google-genai).

사용법:
  python trends/scripts/analyze.py          # 실제 분석 (GEMINI_API_KEY 필요)
  python trends/scripts/analyze.py --mock   # 키 없이 가짜 응답으로 흐름 시험

요청 수: 장르 수(5) + 전체 요약(1) = 하루 6회 (재시도 포함 최대 약 12회)
실패해도 프로그램은 멈추지 않고, 수집 요약만 담은 analysis.md를 만든 뒤
status.json에 실패 사실을 남긴다 → notify.py가 텔레그램으로 알려 줌.
"""
import argparse
import json
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
- 반드시 지정한 JSON 형식으로만 답한다."""

GENRE_SCHEMA = """{
  "videos": [
    {
      "video_id": "영상 ID 그대로",
      "hook": "제목 훅 방식 (예: 반전 예고, 대사 인용형, 질문형, 숫자형)",
      "structure": "추정 이야기 구조 (예: 무시 → 반전 → 사이다)",
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
    {"title": "회차 제목 예시", "background": "배경", "hook": "첫 3초 훅", "structure": "구조 (예: 무시 → 반전 → 사이다)", "length": "추천 길이"}
  ]
}"""


# ---------------- 제미나이 호출 ----------------

class Gemini:
    def __init__(self, cfg, mock=False):
        self.cfg = cfg["gemini"]
        self.mock = mock
        self.requests = 0
        self.last = 0.0
        self.model_used = self.cfg["model"]
        self.dead = set()   # 한도 초과로 끝까지 실패한 모델은 그날 다시 쓰지 않음
        if not mock:
            from google import genai
            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY가 없습니다 (깃허브 Secrets 확인)")
            self.client = genai.Client(api_key=key)

    def _wait(self):
        gap = self.cfg["wait_between_requests_sec"] - (time.time() - self.last)
        if gap > 0 and self.requests:
            time.sleep(gap)
        self.last = time.time()

    def _call(self, model, parts):
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
        last_err = RuntimeError("모든 모델이 오늘 한도 초과")
        for model in [m for m in models if m not in self.dead]:
            try:
                for attempt in range(2):
                    text = self._call(model, parts if attempt == 0 else parts + [
                        "직전 답이 올바른 JSON이 아니었습니다. 설명 없이 JSON만 다시 보내 주세요."])
                    data = parse_json(text)
                    if data is not None:
                        self.model_used = model
                        return data
                    print("[분석] JSON 형식이 깨져서 한 번 더 요청합니다")
                raise ValueError("JSON 형식 오류가 2번 연속 발생")
            except Exception as e:
                last_err = e
                if getattr(e, "code", None) == 429:
                    self.dead.add(model)
                print(f"[분석] 모델 {model} 실패: {scrub(e)[:200]}")
        raise last_err


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
            "structure": "무시당함 → 정체 공개 → 사이다 응징 (추정·샘플)",
            "thumbnail": "놀란 표정 클로즈업 + 노란 큰 글자 (샘플)",
            "reaction": "반전 순간과 다음편 요청에 반응 (샘플)",
            "series": "회차 번호 있음, 다음편 유도 (샘플)" if "화" in v["title"] or "EP" in v["title"] else "단편 (샘플)",
            "take": "첫 3초에 결말 일부를 보여주기 (샘플)", "change": "배경을 배달 현장으로 바꾸기 (샘플)",
        } for i, v in enumerate(vids)],
        "patterns": {"title": f"{genre}: 결과 먼저 말하는 제목 (샘플)", "length": "50~70초 (샘플)",
                     "story": "무시 → 반전 → 사이다 (샘플)", "rhythm": "2~3초마다 컷 (샘플)"},
    }


def mock_summary(bg):
    return {
        "common": [{"name": "제목 패턴", "detail": "결과·반전을 제목에 먼저 공개 (샘플)"},
                   {"name": "길이대", "detail": "50~70초가 가장 많음 (샘플)"},
                   {"name": "이야기 구조", "detail": "무시 → 정체 공개 → 응징 (샘플)"}],
        "rising": ["회차 번호를 제목 앞에 붙이는 형식 (샘플)"], "cooling": [],
        "ideas": [{"title": f"{b}인 줄 알았던 남자의 정체 (샘플)", "background": b, "hook": "무시하는 한마디로 시작",
                   "structure": "무시 → 반전 → 사이다", "length": "60초"} for b in bg[:3]],
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
            L.append(f"{v['rank']}. [{v['title']}]({v['url']}) — 조회 {fmt_num(v['views'])}, {fmt_dur(v['duration_sec'])}")
        L.append("")

    if summary:
        L += ["## 반복해서 나타나는 공통 구조", ""]
        for c in summary.get("common", []):
            L.append(f"- **{c.get('name')}**: {c.get('detail')}")
        L += ["", "## 지난 7일 대비", "", "**새로 뜨는 형식**"]
        L += [f"- {x}" for x in summary.get("rising", [])] or ["- (자료 부족)"]
        L += ["", "**식는 형식**"]
        L += [f"- {x}" for x in summary.get("cooling", [])] or ["- (아직 비교할 과거 자료가 부족)"]
        L += ["", "## 내일 바로 써먹을 회차 아이디어", ""]
        for i, x in enumerate(summary.get("ideas", []), 1):
            L.append(f"{i}. **{x.get('title')}** ({x.get('background')}, {x.get('length')})")
            L.append(f"   - 훅: {x.get('hook')} / 구조: {x.get('structure')}")
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
            prompt = (f"너는 숏드라마 형식 분석가다. 장르: {g}\n"
                      f"아래는 최근 7일 한국 유튜브 쇼츠 인기 영상 {len(gv)}개의 공개 정보다.\n"
                      f"함께 붙인 썸네일 이미지 {len(from_img)}장은 순서대로 이 영상들의 것이다: {from_img}\n"
                      f"모든 영상에 대해 템플릿을 채워라.\n{RULES}\n\n"
                      f"영상 정보(JSON):\n{json.dumps([video_brief(v) for v in gv], ensure_ascii=False)}\n\n"
                      f"답 형식(JSON):\n{GENRE_SCHEMA}")
            try:
                genres[g] = gm.ask_json(parts + [prompt], lambda: mock_genre(g, gv))
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
                      "- rising/cooling: past_7_days와 비교. 과거 자료가 없으면 오늘 상승 속도(fast_rising) 기준으로 rising만 쓰고 cooling은 빈 목록\n"
                      f"- ideas: 내일 바로 찍을 회차 아이디어 정확히 3개. 배경은 {bg} 중에서 하나씩 골라 바꿔라. "
                      "남의 영상 줄거리를 그대로 옮기지 말고 형식만 빌려 새 이야기로.\n"
                      f"{RULES}\n\n자료(JSON):\n{json.dumps(ctx, ensure_ascii=False)}\n\n답 형식(JSON):\n{SUMMARY_SCHEMA}")
            try:
                summary = gm.ask_json([prompt], lambda: mock_summary(bg))
                print("[분석] 전체 요약 완료")
            except Exception as e:
                failures.append(f"전체 요약 실패({scrub(e)[:80]})")

    model = gm.model_used if gm else "-"
    reqs = gm.requests if gm else 0
    if args.mock:
        model += " (가짜 응답)"
    md = render_md(today, raw, genres, summary, failures, model, reqs)
    (d / "analysis.md").write_text(md, encoding="utf-8")
    write_json(d / "analysis.json", {"date": today, "sample": bool(raw.get("sample")), "model": model, "requests": reqs,
                                     "genres": genres, "summary": summary, "failures": failures})
    print(f"[분석] 제미나이 요청 {reqs}회 · 모델 {model}")
    ok = bool(summary) and not failures
    write_status(today, "analyze", ok, " / ".join(failures) if failures else f"요청 {reqs}회")
    if failures:
        print("[분석 일부 실패] " + " / ".join(failures), file=sys.stderr)
    # 분석이 실패해도 0으로 끝내서 전송 단계가 이어지게 한다 (실패는 status.json으로 전달)


if __name__ == "__main__":
    main()
