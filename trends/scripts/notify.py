"""텔레그램 전송.

사용법:
  python trends/scripts/notify.py                    # 오늘 요약 전송
  python trends/scripts/notify.py --dry-run          # 보내지 않고 화면에 출력
  python trends/scripts/notify.py --fail "수집: 이유"  # 실패 알림 한 줄
"""
import argparse
import html
import os
import sys

import requests

from common import day_dir, fmt_dur, fmt_num, load_config, read_json, scrub, today_kst

LIMIT = 4096


def esc(s):
    return html.escape(str(s or ""), quote=False)


def report_url(date):
    repo = os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    if not repo:
        return None
    return f"https://github.com/{repo}/blob/{branch}/trends/data/{date}/analysis.md"


def build_message(date, cfg):
    d = day_dir(date)
    raw = read_json(d / "raw.json")
    an = read_json(d / "analysis.json") if (d / "analysis.json").exists() else {}
    status = read_json(d / "status.json") if (d / "status.json").exists() else {}
    summary = an.get("summary") or {}
    n = cfg["telegram"]["top_links_per_genre"]
    vids = raw["videos"]

    L = [f"<b>📊 숏폼 유행 {date}</b>"]
    if raw.get("sample"):
        L.append("⚠️ 샘플 데이터 시험 전송")
    a = status.get("analyze", {})
    if a and not a.get("ok"):
        L.append(f"⚠️ 분석 실패 → 수집 요약만 보냅니다\n사유: {esc(a.get('message'))}")

    drama = [v for v in vids if (v.get("kind") or "drama") == "drama"]
    L.append("\n<b>🔥 장르별 상위 3</b>")
    for g in dict.fromkeys(v["genre"] for v in drama):
        L.append(f"\n<b>[{esc(g)}]</b>")
        for v in [x for x in vids if x["genre"] == g][:n]:
            title = v["title"][:40] + ("…" if len(v["title"]) > 40 else "")
            ratio = f" · 구독자대비 {v['view_sub_ratio']}배" if v.get("view_sub_ratio") else ""
            L.append(f'{v["rank"]}. <a href="{esc(v["url"])}">{esc(title)}</a>\n'
                     f'   {fmt_num(v["views"])}회 · {fmt_dur(v["duration_sec"])}{ratio}')
    # 인기 숏폼은 장르가 많아 한국·해외로 묶어 조회수 상위만 보낸다
    for market, name in (("kr", "🎬 인기 숏폼 · 한국"), ("global", "🌍 인기 숏폼 · 해외")):
        pop = sorted([v for v in vids if v.get("kind") == "popular" and v.get("market", "kr") == market],
                     key=lambda v: v["views"], reverse=True)[:cfg["telegram"].get("popular_links", 5)]
        if not pop:
            continue
        L.append(f"\n<b>{name}</b>")
        for i, v in enumerate(pop, 1):
            title = v["title"][:40] + ("…" if len(v["title"]) > 40 else "")
            L.append(f'{i}. <a href="{esc(v["url"])}">{esc(title)}</a>\n'
                     f'   {fmt_num(v["views"])}회 · {fmt_dur(v["duration_sec"])} · {esc(v["genre"].split("·", 1)[-1].strip())}')
    if not vids:
        L.append("(조건에 맞는 영상이 없었습니다)")

    if summary.get("common"):
        L.append("\n<b>🧩 공통 구조 3가지</b>")
        for c in summary["common"][:3]:
            L.append(f"• <b>{esc(c.get('name'))}</b>: {esc(c.get('detail'))}")
    if summary.get("rising"):
        L.append("\n<b>📈 새로 뜨는 형식</b>")
        L += [f"• {esc(x)}" for x in summary["rising"][:3]]
    if summary.get("popular"):
        L.append("\n<b>🎯 인기 숏폼에서 잘 되는 형식</b>")
        L += [f"• {esc(x)}" for x in summary["popular"][:3]]
    if summary.get("ideas"):
        L.append("\n<b>💡 내일 찍을 아이디어</b>")
        for i, x in enumerate(summary["ideas"][:3], 1):
            L.append(f"{i}. <b>{esc(x.get('title'))}</b>\n   훅: {esc(x.get('hook'))}\n   구조: {esc(x.get('structure'))}")

    q = raw.get("quota", {}).get("used_units")
    L.append(f"\n유튜브 {q if q is not None else '-'}단위 · 제미나이 {an.get('requests', 0)}회"
             + (f" · 직접 본 영상 {an['watched']}개" if an.get("watched") else ""))
    url = report_url(date)
    if url:
        L.append(f'📄 <a href="{url}">전체 분석 보기</a>')
    return "\n".join(L)


def split(text, limit=LIMIT):
    """줄 단위로 4,096자 이하 조각으로 나눈다."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # 한 줄이 너무 긴 경우
            parts.append(cur) if cur else None
            cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send(text, dry=False):
    chunks = split(text)
    if dry:
        for i, c in enumerate(chunks, 1):
            print(f"----- 메시지 {i}/{len(chunks)} ({len(c)}자) -----\n{c}")
        return
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID가 없습니다 (깃허브 Secrets 확인)")
    for c in chunks:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30,
                              data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"})
        except requests.RequestException as e:
            raise RuntimeError(f"텔레그램 연결 실패: {scrub(e)}") from None
        if r.status_code != 200:
            desc = scrub(r.json().get("description", "")) if r.headers.get("content-type", "").startswith("application/json") else ""
            raise RuntimeError(f"텔레그램 전송 실패 {r.status_code} {desc}")
    print(f"[전송] 텔레그램 메시지 {len(chunks)}개 보냄")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fail", help="실패 알림 한 줄")
    args = ap.parse_args()
    try:
        if args.fail:
            send(f"❌ 숏폼 자동화 실패 ({today_kst()})\n{esc(args.fail)}", args.dry_run)
            return
        send(build_message(today_kst(), load_config()), args.dry_run)
    except Exception as e:
        print(f"[전송 실패] {scrub(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
