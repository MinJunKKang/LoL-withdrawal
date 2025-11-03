# utils/stats.py
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

# ── 데이터 경로
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATS_PATH = DATA_DIR / "user_stats.json"

# ── 기본 레코드 (이번 서버에서 쓰는 키만)
DEFAULT_USER = {
    "참여": 0,
    "승리": 0,
    "패배": 0,
    "포인트": 0,
    "경험치": 0,
    "출석_마지막": None,   # "YYYY-MM-DD"
    "히스토리": [],        # 최근 경기 결과 기록: 1(승) / 0(패)
    "도박_최근": None,     # 마지막 도박 시간(ISO 8601, UTC)
}

# ── JSON helpers
def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

def _write_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_stats() -> dict:
    return _read_json(STATS_PATH)

def save_stats(data: dict) -> None:
    _write_json(STATS_PATH, data)

def ensure_user(stats: dict, uid: str) -> dict:
    rec = stats.get(uid)
    if rec is None:
        rec = DEFAULT_USER.copy()
        stats[uid] = rec
    else:
        for k, v in DEFAULT_USER.items():
            rec.setdefault(k, v)
    return rec

def format_num(n: int | float) -> str:
    return f"{n:,}"

# ── 내전 결과 저장(히스토리 포함)
def update_result_dual(user_id: int | str, won: bool) -> None:
    uid = str(user_id)
    stats = load_stats()
    rec = ensure_user(stats, uid)

    rec["참여"] = int(rec.get("참여", 0)) + 1
    if won:
        rec["승리"] = int(rec.get("승리", 0)) + 1
        rec["히스토리"].append(1)
    else:
        rec["패배"] = int(rec.get("패배", 0)) + 1
        rec["히스토리"].append(0)

    # 히스토리는 너무 커지지 않게 뒤에서 200개만 유지
    if len(rec["히스토리"]) > 200:
        rec["히스토리"] = rec["히스토리"][-200:]

    save_stats(stats)

# ── 포인트 helpers
def get_points(user_id: int | str) -> int:
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    return int(rec.get("포인트", 0))

def add_points(user_id: int | str, amount: int) -> int:
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    rec["포인트"] = max(0, int(rec.get("포인트", 0)) + int(amount))
    save_stats(stats)
    return rec["포인트"]

def can_spend_points(user_id: int | str, amount: int) -> bool:
    return get_points(user_id) >= int(amount)

def spend_points(user_id: int | str, amount: int) -> bool:
    amount = int(amount)
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    if rec.get("포인트", 0) < amount:
        return False
    rec["پو인트" if "포인트" not in rec else "포인트"] = int(rec.get("포인트", 0)) - amount
    save_stats(stats)
    return True

# ── 도박(개인 쿨타임) helpers
def _parse_iso_or_none(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # 저장은 UTC ISO 형태로 함
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def get_last_gamble(user_id: int | str) -> datetime | None:
    """마지막 도박 시각(UTC, aware) 또는 None 반환."""
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    return _parse_iso_or_none(rec.get("도박_최근"))

def set_last_gamble(user_id: int | str, when: datetime | None = None) -> None:
    """마지막 도박 시각을 기록. 기본은 지금(UTC)."""
    stats = load_stats()
    rec = ensure_user(stats, str(user_id))
    when = when or datetime.now(timezone.utc)
    rec["도박_최근"] = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    save_stats(stats)

def gamble_cooldown_remaining(user_id: int | str, hours: int = 12) -> int:
    """
    남은 쿨타임(초) 반환. 쿨타임이 없으면 0.
    economy.py에서 수동 체크용으로 사용 가능.
    """
    last = get_last_gamble(user_id)
    if not last:
        return 0
    now = datetime.now(timezone.utc)
    until = last + timedelta(hours=hours)
    remain = int((until - now).total_seconds())
    return max(0, remain)
