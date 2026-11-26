"""실패 알림용: 오늘 status.json에서 실패 이유 한 줄을 꺼낸다 (워크플로 전용)."""
import os

from common import DATA, read_json, today_kst

p = DATA / today_kst() / "status.json"
if p.exists():
    st = read_json(p).get(os.environ.get("KEY", ""), {})
    if st and not st.get("ok"):
        print(st.get("message", ""))
