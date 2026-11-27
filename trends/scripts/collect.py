"""① 유행 수집 — YouTube Data API v3 공식 연동만 사용.
숏드라마 장르(genres·global_genres)와 인기 숏폼(popular: 검색 + 인기 급상승 차트)을 함께 모은다.

사용법:
  python trends/scripts/collect.py            # 실제 수집 (YOUTUBE_API_KEY 필요)
  python trends/scripts/collect.py --sample   # 키 없이 샘플 데이터로 시험

할당량(단위) 계산:
  search.list        100 / 회
  videos.list          1 / 회 (영상 50개씩)
  channels.list        1 / 회 (채널 50개씩)
  commentThreads.list  1 / 회 (영상 1개)
"""
import argparse
import csv
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

import requests

from common import (DATA, ROOT, day_dir, load_config, read_json, scrub,
                    today_kst, write_json, write_status)

API = "https://www.googleapis.com/youtube/v3/"
COST = {"search": 100, "videos": 1, "channels": 1, "commentThreads": 1}
HISTORY = DATA / "history.csv"
HISTORY_FIELDS = ["date", "video_id", "genre", "views", "likes", "comments"]


class YouTube:
    def __init__(self, key, budget):
        self.s = requests.Session()
        # 키는 주소(URL)가 아닌 헤더로 보낸다 → 오류 로그에 키가 찍히지 않음
        self.s.headers["X-Goog-Api-Key"] = key
        self.used = 0
        self.budget = budget
        self.calls = {}

    def can_spend(self, units):
        return self.used + units <= self.budget

    def get(self, endpoint, **params):
        self.used += COST[endpoint]
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1
        r = self.s.get(API + endpoint, params=params, timeout=30)
        if r.status_code != 200:
            reason = ""
            try:
                err = r.json()["error"]
                reason = err.get("errors", [{}])[0].get("reason", "") or err.get("message", "")
            except Exception:
                pass
            raise YouTubeError(r.status_code, reason)
        return r.json()


class YouTubeError(Exception):
    def __init__(self, code, reason):
        super().__init__(f"YouTube API {code} {reason}")
        self.code, self.reason = code, reason


def parse_duration(iso):
    """PT1M30S -> 90"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ---------------- 누적 기록(history.csv) ----------------

def load_history():
    if not HISTORY.exists():
        return []
    with open(HISTORY, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_history(rows, retention_days, today):
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    rows = [r for r in rows if r["date"] >= cutoff]
    rows.sort(key=lambda r: (r["date"], r["video_id"]))
    with open(HISTORY, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(rows)


def velocity(history, vid, views, today, published):
    """하루 조회수 증가량. 어제 기록이 있으면 실제 증가량, 없으면 올린 뒤 하루 평균."""
    past = [r for r in history if r["video_id"] == vid and r["date"] < today]
    if past:
        last = max(past, key=lambda r: r["date"])
        gap = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last["date"], "%Y-%m-%d")).days or 1
        return round((views - int(last["views"])) / gap), "실측"
    pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
    days = max((datetime.now(timezone.utc) - pub).total_seconds() / 86400, 0.5)
    return round(views / days), "평균"


def prune_old_dirs(retention_days, today):
    """오래된 원본은 지운다 (유튜브 데이터는 30일 안에 새로 받는 것이 원칙)."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    for d in DATA.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name < cutoff:
            raw = d / "raw.json"
            if raw.exists():
                raw.unlink()


# ---------------- 수집 ----------------

def genre_table(cfg):
    """(장르, 검색어들, 지역, 언어, 시장 kr/global, 종류 drama/popular) 목록.
    숏드라마 장르가 먼저, 인기 숏폼은 세부 장르 → 종합 순서 (먼저 찾은 장르에 영상을 둔다)."""
    yc, gc, pc = cfg["youtube"], cfg.get("global", {}), cfg.get("popular") or {}
    kr = (yc["region"], yc["language"])
    gl = (gc.get("region", "US"), gc.get("language", "en"))
    rows = [(g, q or [], *kr, "kr", "drama") for g, q in cfg["genres"].items()]
    rows += [(g, q or [], *gl, "global", "drama") for g, q in (cfg.get("global_genres") or {}).items()]
    rows += [(g, q or [], *kr, "kr", "popular") for g, q in (pc.get("kr") or {}).items()]
    rows += [(g, q or [], *gl, "global", "popular") for g, q in (pc.get("global") or {}).items()]
    return rows


def chart_targets(cfg):
    """인기 급상승 차트 결과를 넣을 (장르, 지역). 시장별 인기 숏폼의 마지막 장르(종합)에 넣는다."""
    yc, gc, pc = cfg["youtube"], cfg.get("global", {}), cfg.get("popular") or {}
    out = []
    if pc.get("kr"):
        out.append((list(pc["kr"])[-1], yc["region"]))
    if pc.get("global"):
        out.append((list(pc["global"])[-1], gc.get("region", "US")))
    return out


def collect(cfg, key, today):
    yc = cfg["youtube"]
    yt = YouTube(key, yc["quota_budget"])
    after = (datetime.now(timezone.utc) - timedelta(days=yc["days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    log = []

    # 1) 장르별 검색 (중복 제거: 먼저 찾은 장르에 둔다)
    found = {}          # video_id -> genre
    markets = genre_table(cfg)
    info = {g: (market, kind) for g, _, _, _, market, kind in markets}
    for genre, queries, region, lang, _, _ in markets:
        for q in queries:
            if not yt.can_spend(COST["search"] + 60):   # 상세조회·댓글용 여유분 남김
                log.append(f"할당량 예산 초과 우려로 검색 중단: '{q}'")
                break
            res = yt.get("search", part="id", q=q, type="video", videoDuration="short",
                         regionCode=region, relevanceLanguage=lang,
                         publishedAfter=after, order="viewCount",
                         maxResults=min(int(yc["results_per_search"]), 50))
            for it in res.get("items", []):
                vid = it["id"].get("videoId")
                if vid and vid not in found:
                    found[vid] = genre
            log.append(f"검색 '{q}' → 누적 {len(found)}개")

    # 1-2) 인기 급상승 차트 (지역당 50개에 1단위). 3분 이하만 뒤에서 걸러 "인기 · 종합"에 넣는다
    pc = cfg.get("popular") or {}
    if pc.get("chart"):
        for genre, region in chart_targets(cfg):
            token = None
            for _ in range(int(pc.get("chart_pages", 2))):
                if not yt.can_spend(COST["videos"] + 60):
                    break
                params = dict(part="id", chart="mostPopular", regionCode=region, maxResults=50)
                if token:
                    params["pageToken"] = token
                try:
                    res = yt.get("videos", **params)
                except YouTubeError as e:
                    log.append(f"인기 차트({region}) 건너뜀: {e.code} {e.reason}")
                    break
                for it in res.get("items", []):
                    found.setdefault(it["id"], genre)
                token = res.get("nextPageToken")
                if not token:
                    break
            log.append(f"인기 차트 {region} → 누적 {len(found)}개")

    # 2) 상세 정보 (지난 7일 동안 추적한 영상도 함께 갱신 → 상승 속도 계산)
    history = load_history()
    week_ago = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    tracked = {r["video_id"]: r["genre"] for r in history if r["date"] >= week_ago}
    ids = list(found) + [v for v in tracked if v not in found]
    details = {}
    for part in chunks(ids, 50):
        res = yt.get("videos", part="snippet,contentDetails,statistics", id=",".join(part), maxResults=50)
        for it in res.get("items", []):
            details[it["id"]] = it

    # 오늘 조회수를 누적 기록에 쌓기 (추적 중인 영상 전부)
    history = [r for r in history if r["date"] != today]
    for vid, it in details.items():
        st = it.get("statistics", {})
        history.append({"date": today, "video_id": vid, "genre": found.get(vid) or tracked.get(vid, ""),
                        "views": st.get("viewCount", 0), "likes": st.get("likeCount", 0),
                        "comments": st.get("commentCount", 0)})

    # 3) 조건 거르기: 오늘 검색된 영상 중 3분 이하, 최소 조회수 이상
    videos = []
    for vid, genre in found.items():
        it = details.get(vid)
        if not it:
            continue
        sec = parse_duration(it["contentDetails"].get("duration"))
        views = int(it.get("statistics", {}).get("viewCount", 0))
        market, kind = info.get(genre, ("kr", "drama"))
        max_sec = pc.get("max_duration_sec", yc["max_duration_sec"]) if kind == "popular" else yc["max_duration_sec"]
        min_views = pc.get("min_views", yc["min_views"]) if kind == "popular" else yc["min_views"]
        if sec == 0 or sec > max_sec or views < min_views:
            continue
        sn, st = it["snippet"], it.get("statistics", {})
        desc = sn.get("description", "")
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        tags = re.findall(r"#[\w가-힣]+", sn.get("title", "") + " " + desc)
        videos.append({
            "genre": genre, "market": market, "kind": kind, "video_id": vid, "url": f"https://www.youtube.com/shorts/{vid}",
            "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
            "channel_id": sn.get("channelId", ""), "published_at": sn.get("publishedAt", ""),
            "duration_sec": sec, "views": views, "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)), "description_head": desc[:200],
            "hashtags": list(dict.fromkeys(tags))[:15], "thumbnail": thumb,
        })

    # 4) 채널 구독자 수 → 구독자 대비 조회수
    ch_ids = list({v["channel_id"] for v in videos})
    subs = {}
    for part in chunks(ch_ids, 50):
        res = yt.get("channels", part="statistics", id=",".join(part), maxResults=50)
        for it in res.get("items", []):
            st = it.get("statistics", {})
            subs[it["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    for v in videos:
        s = subs.get(v["channel_id"])
        v["subscribers"] = s
        v["view_sub_ratio"] = round(v["views"] / s, 1) if s else None
        v["velocity"], v["velocity_type"] = velocity(history, v["video_id"], v["views"], today, v["published_at"])

    # 5) 장르별 상위 N개
    key_fn = {"views": lambda v: v["views"], "ratio": lambda v: v["view_sub_ratio"] or 0,
              "velocity": lambda v: v["velocity"]}[yc.get("rank_by", "views")]
    top = []
    for genre, (market, kind) in info.items():
        g = sorted([v for v in videos if v["genre"] == genre], key=key_fn, reverse=True)
        n_top = pc.get("top_per_genre", yc["top_per_genre"]) if kind == "popular" else yc["top_per_genre"]
        for rank, v in enumerate(g[:n_top], 1):
            v["rank"] = rank
            top.append(v)

    # 6) 인기 댓글 (상위 영상만 → 영상당 1단위)
    for v in top:
        v["top_comments"] = []
        if v["comments"] == 0 or not yt.can_spend(1):
            continue
        try:
            res = yt.get("commentThreads", part="snippet", videoId=v["video_id"], order="relevance",
                         maxResults=yc["comments_per_video"], textFormat="plainText")
        except YouTubeError as e:
            if e.code in (403, 404):   # 댓글 막힘 등
                continue
            raise
        for it in res.get("items", []):
            c = it["snippet"]["topLevelComment"]["snippet"]
            v["top_comments"].append({"text": c.get("textDisplay", "")[:200], "likes": c.get("likeCount", 0)})

    save_history(history, yc["retention_days"], today)
    prune_old_dirs(yc["retention_days"], today)
    return top, {"used_units": yt.used, "calls": yt.calls, "log": log}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="키 없이 샘플 데이터 사용")
    args = ap.parse_args()
    cfg = load_config()
    today = today_kst()
    out = day_dir(today) / "raw.json"

    try:
        if args.sample:
            sample = read_json(ROOT / "sample" / "raw.json")
            sample["date"] = today
            sample["sample"] = True
            write_json(out, sample)
            print(f"[수집] 샘플 데이터 사용 → {out.relative_to(ROOT.parent)}")
            write_status(today, "collect", True, "샘플")
            return
        key = os.environ.get("YOUTUBE_API_KEY")
        if not key:
            raise RuntimeError("YOUTUBE_API_KEY가 없습니다 (깃허브 Secrets 확인)")
        top, quota = collect(cfg, key, today)
        write_json(out, {"date": today, "sample": False, "quota": quota, "videos": top})
        for line in quota["log"]:
            print("[수집]", line)
        print(f"[수집] 영상 {len(top)}개 저장 → {out.relative_to(ROOT.parent)}")
        print(f"[수집] 사용한 할당량: {quota['used_units']}단위 / 10,000 (호출: {quota['calls']})")
        write_status(today, "collect", True, f"{len(top)}개, {quota['used_units']}단위")
    except Exception as e:
        msg = scrub(e)
        if isinstance(e, YouTubeError) and e.reason == "quotaExceeded":
            msg = "유튜브 하루 할당량을 다 썼습니다 (내일 오후 4~5시 초기화)"
        print(f"[수집 실패] {msg}", file=sys.stderr)
        write_status(today, "collect", False, msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
