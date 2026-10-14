"""하루에 한 번, 창이 뜬 뒤 책갈피 툴 사이트에 새 버전이 있는지 묻는다.

보내는 것은 도구 이름(slug), 버전, "win" 뿐이다. 파일·경로·기기·사용자 정보는 보내지 않는다.
요청은 창이 뜬 1.5초 뒤 백그라운드 스레드에서 보내고 화면을 막지 않는다. 실패(오프라인, 시간 초과,
HTTP 오류, 잘못된 JSON)는 조용히 무시한다. 새 버전인지는 서버가 정하고(updateAvailable),
true 일 때만 알림을 띄운다. 표준 라이브러리만 쓴다.

설정은 TrimPDF 의 settings.json 사전(dict)을 그대로 받는다. 파일은 누구나 고칠 수 있으므로
꺼내 쓸 때마다 자료형을 확인하고, 어긋나면 기본값을 쓴다.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import threading
import urllib.parse
import urllib.request
from typing import Callable

API = "https://chaekgalpi.co.kr/api/tools/{slug}/latest"
SITE = "https://chaekgalpi.co.kr/"      # 알림은 이 사이트의 페이지만 연다
TIMEOUT = 4.0
DELAY_MS = 1500
POLL_MS = 100           # Tk 쪽이 스레드의 답을 들여다보는 간격
MAX_BYTES = 64 * 1024


@dataclasses.dataclass
class UpdateInfo:
    latest: str
    current: str
    url: str


def today() -> str:
    return datetime.date.today().isoformat()


def enabled(prefs) -> bool:
    """「새 버전 확인」이 켜져 있는가. 기본은 켜짐이고, 불리언이 아닌 값은 기본값으로 본다."""
    value = prefs.get("update_check", True) if isinstance(prefs, dict) else True
    return value if isinstance(value, bool) else True


def _text(prefs, key: str, limit: int) -> str:
    value = prefs.get(key, "") if isinstance(prefs, dict) else ""
    return value.strip()[:limit] if isinstance(value, str) else ""


def last_checked(prefs) -> str:
    return _text(prefs, "update_check_last", 10)


def skipped_version(prefs) -> str:
    return _text(prefs, "update_check_skipped", 32)


def should_check(prefs, day: str) -> bool:
    """켜져 있고, 오늘 아직 묻지 않았는가."""
    return enabled(prefs) and last_checked(prefs) != day


def fetch(slug: str, version: str, *, opener: Callable = urllib.request.urlopen) -> dict | None:
    """서버의 답. 어떤 이유로든 실패하면 None."""
    query = urllib.parse.urlencode({"v": version, "os": "win"})
    req = urllib.request.Request(f"{API.format(slug=slug)}?{query}",
                                 headers={"User-Agent": f"chaekgalpi-tool/{slug}/{version}",
                                          "Accept": "application/json"})
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read(MAX_BYTES).decode("utf-8"))
    except Exception:      # 오프라인, 시간 초과, HTTP 오류, 잘못된 JSON: 아무 말도 하지 않는다
        return None
    return data if isinstance(data, dict) else None


def interpret(data: dict | None, version: str, skipped: str = "") -> UpdateInfo | None:
    """서버가 새 버전이 있다고 하고, 사용자가 건너뛴 버전이 아니고, 링크가 책갈피 툴 사이트일 때만 알림 정보를 돌려준다."""
    if not isinstance(data, dict) or data.get("updateAvailable") is not True:
        return None
    latest, url = data.get("latest"), data.get("url")
    if not isinstance(latest, str) or not latest.strip() or len(latest) > 32:
        return None
    if not isinstance(url, str) or not url.startswith(SITE):
        return None
    latest = latest.strip()
    if latest == (skipped or "").strip():
        return None
    return UpdateInfo(latest, version, url)


def check_async(root, slug: str, version: str, prefs, on_result: Callable[[UpdateInfo | None, bool], None], *,
                fetcher: Callable = fetch, day: str | None = None) -> bool:
    """DELAY_MS 뒤에 한 번 확인하도록 예약한다. 스레드는 결과를 상자에 담을 뿐이고, on_result(info, answered) 는
    Tk 쪽이 상자를 보고 Tk 스레드에서 부른다.
    answered 는 서버가 답했는가(그때만 호출한 쪽이 날짜를 기록한다), info 는 알림을 띄울 때만 값이 있다.
    확인할 차례가 아니면 False."""
    if not should_check(prefs, day or today()):
        return False
    skipped = skipped_version(prefs)
    box: dict = {}

    # 스레드는 결과를 box 에만 담고 Tk 객체나 콜백을 쥐지 않는다. 예전에는 스레드가 root.after(0, 콜백)으로
    # 결과를 넘겼는데, 요청이 나가 있는 동안 창을 닫으면 스레드가 앱의 마지막 참조를 쥐게 되고, 스레드가
    # 끝나는 순간 Tk 변수가 주 스레드 밖에서 해제되어 Tcl 이 중단됐다("Tcl_AsyncDelete: async handler
    # deleted by the wrong thread"). 그래서 결과는 Tk 쪽이 POLL_MS 마다 상자를 들여다보고 가져간다.
    def work(box=box) -> None:
        try:
            box["data"] = fetcher(slug, version)
        except Exception:      # 어떤 이유로든 실패하면 '답 없음'. 상자는 반드시 채워서 Tk 쪽이 기다리다 멈추게 한다
            box["data"] = None

    def poll() -> None:
        if "data" not in box:
            try:
                root.after(POLL_MS, poll)
            except Exception:      # 그 사이 창이 닫혔다
                pass
            return
        data = box["data"]
        on_result(interpret(data, version, skipped), data is not None)

    def start() -> None:
        threading.Thread(target=work, name="update-check", daemon=True).start()
        poll()

    try:
        root.after(DELAY_MS, start)
    except Exception:
        return False
    return True
