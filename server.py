"""Laser Quiz - serveur FastAPI + WebSockets (multi-salles)."""

from __future__ import annotations

import asyncio
import base64
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
QUESTIONS_FILE = BASE_DIR / "questions.json"

# Durées des phases (en secondes "de jeu")
LOBBY_DURATION = 240
QUESTION_DURATION = 20
REVEAL_DURATION = 10
INTER_PODIUM_DURATION = 10
PODIUM_DURATION = 60
QUESTIONS_PER_GAME = 15
CHECKPOINT_INTERVAL = 5

MIN_POINTS = 500
MAX_POINTS = 1000

CODE_LENGTH = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans caractères ambigus (O/0, I/1)
ROOM_TTL = 30 * 60  # suppression d'une salle vide après 30 minutes
MAX_NAME_LENGTH = 16
DISCONNECT_GRACE_LOBBY = 5.0
DISCONNECT_GRACE_GAME = 30.0
AFK_MISSED_LIMIT = 5

PHOTOBOOTH_CACHE_TTL = 90.0
PHOTOBOOTH_CACHE: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
DEFAULT_STORES: list[dict[str, str]] = [
    {"name": "Sainte-Foy", "url": "https://www.simplebooth.com/gallery/3DTLTk68fwVM-lge-fev-2026"},
    {"name": "Vanier", "url": "https://www.simplebooth.com/gallery/8KonRrq3avT5-lge-fev-26"},
    {"name": "Brossard", "url": "https://www.simplebooth.com/gallery/urjGcth7fSdh-lge-2026"},
    {"name": "Marché Central", "url": "https://www.simplebooth.com/gallery/3vaHgUNcJUos-lge-fev-2026"},
    {"name": "Lévis", "url": "https://www.simplebooth.com/gallery/ahebLG4GWjZF-lge-fev-2026"},
    {"name": "Sherbrooke", "url": "https://www.simplebooth.com/gallery/XCaTpPmn6r3h-lge-fev-2026"},
    {"name": "Ste-Catherine", "url": "https://www.simplebooth.com/gallery/ALkJDisegsdU-lge-fev-2026"},
]

# --------------------------------------------------------------------------- #
# Statistiques & Export automatique Google Sheets
# --------------------------------------------------------------------------- #
GOOGLE_SHEET_ID = "1ZJ-sDjjC74DrQmhNJDXOargA7OH-4HJJ2ecenpQX0sI"
GOOGLE_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/edit?gid=0#gid=0"
STATS_CONFIG_FILE = BASE_DIR / "stats_config.json"
STATS_HEADERS = [
    "Horodatage",
    "Adresse IP de la session",
    "Nombre de joueurs",
    "Nombre de questions répondues par joueur",
    "Temps de jeu par joueur",
    "Combien de partages de stats",
]
GAME_LOGS_HISTORY: list[dict[str, Any]] = []
LAST_PUSH_STATUS: dict[str, Any] = {
    "ok": None,
    "message": "Aucun envoi effectué",
    "at": "",
}

# --------------------------------------------------------------------------- #
# Gestion des utilisateurs (LGE / AdminLGE) & Blocage de page
# --------------------------------------------------------------------------- #
AUTH_CONFIG_FILE = BASE_DIR / "auth_config.json"
DEFAULT_LGE_PASSWORD = os.environ.get("LGE_DEFAULT_PASSWORD", "LGE2026")
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_DEFAULT_PASSWORD", "Admin2026!")
_AUTH_SECRET = os.environ.get("LGE_AUTH_SECRET", "laser-quiz-lge-permanent-secret-2026")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()


def _build_default_auth_state() -> dict[str, Any]:
    lge_salt = secrets.token_hex(16)
    admin_salt = secrets.token_hex(16)
    return {
        "page_lock_enabled": True,
        "users": {
            "LGE": {
                "username": "LGE",
                "role": "user",
                "salt": lge_salt,
                "password_hash": _hash_password(DEFAULT_LGE_PASSWORD, lge_salt),
                "must_change_password": False,
            },
            "AdminLGE": {
                "username": "AdminLGE",
                "role": "admin",
                "salt": admin_salt,
                "password_hash": _hash_password(DEFAULT_ADMIN_PASSWORD, admin_salt),
                "must_change_password": True,
            },
        },
    }


def _save_auth_state(state: dict[str, Any]) -> None:
    AUTH_CONFIG_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_auth_state() -> dict[str, Any]:
    default = _build_default_auth_state()
    if AUTH_CONFIG_FILE.exists():
        try:
            loaded = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                users = loaded.get("users")
                if isinstance(users, dict) and "LGE" in users and "AdminLGE" in users:
                    return {
                        "page_lock_enabled": bool(loaded.get("page_lock_enabled", True)),
                        "users": users,
                    }
        except Exception:
            pass
    return default


AUTH_STATE: dict[str, Any] = _load_auth_state()


def _make_auth_token(username: str, role: str, purpose: str = "session") -> str:
    raw = json.dumps(
        {"u": username, "r": role, "p": purpose},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(
        _AUTH_SECRET.encode("utf-8"),
        b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{b64}.{sig}"


def _verify_auth_token(
    token: str | None, expected_purpose: str = "session"
) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.strip().split(".", 1)
        expected_sig = hmac.new(
            _AUTH_SECRET.encode("utf-8"),
            b64.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padded = b64 + "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("p") != expected_purpose:
            return None
        user = _find_user(str(payload.get("u") or ""))
        if user is None:
            return None
        return payload
    except Exception:
        return None


def _find_user(username: str) -> dict[str, Any] | None:
    low = username.strip().lower()
    for uname, udata in (AUTH_STATE.get("users") or {}).items():
        if uname.lower() == low:
            return udata
    return None


def _now_quebec_str() -> str:
    tz = ZoneInfo("America/Toronto") if ZoneInfo is not None else timezone(timedelta(hours=-5))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float) -> str:
    sec = max(1, round(seconds))
    mins, secs = divmod(sec, 60)
    return f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"


def _extract_client_ip(websocket: WebSocket) -> str:
    xff = websocket.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    xri = websocket.headers.get("x-real-ip", "").strip()
    if xri:
        return xri
    if websocket.client and websocket.client.host:
        return str(websocket.client.host)
    return "Inconnue"


def get_sheets_webhook_url() -> str:
    if STATS_CONFIG_FILE.exists():
        try:
            data = json.loads(STATS_CONFIG_FILE.read_text(encoding="utf-8"))
            url = str(data.get("webhook_url") or "").strip()
            if url:
                return url
        except Exception:
            pass
    return os.environ.get("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()


def set_sheets_webhook_url(url: str) -> str:
    cleaned = url.strip()
    if cleaned and not cleaned.startswith(("https://", "http://")):
        raise ValueError("URL Webhook invalide.")
    STATS_CONFIG_FILE.write_text(
        json.dumps({"webhook_url": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cleaned


def _record_game_history(payload: dict[str, Any]) -> None:
    game_id = payload.get("game_id")
    entry = {
        "game_id": game_id,
        "row": list(payload.get("row") or []),
        "category": payload.get("category", ""),
        "difficulty": payload.get("difficulty", ""),
    }
    for i, existing in enumerate(GAME_LOGS_HISTORY):
        if existing.get("game_id") == game_id:
            GAME_LOGS_HISTORY[i] = entry
            return
    GAME_LOGS_HISTORY.append(entry)
    if len(GAME_LOGS_HISTORY) > 200:
        del GAME_LOGS_HISTORY[:-200]


def _push_sheets_sync(webhook_url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = getattr(resp, "status", 200)
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if status >= 400:
                return False, f"HTTP {status}: {raw[:160]}"
            try:
                parsed = json.loads(raw) if raw else {}
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    return False, str(parsed.get("error") or "Erreur Apps Script")
            except json.JSONDecodeError:
                pass
            return True, f"Synchro OK ({payload['row'][0]})"
    except urllib.error.HTTPError as err:
        return False, f"HTTP {err.code}"
    except Exception as exc:
        return False, str(exc)[:160]


async def push_game_stats_async(payload: dict[str, Any]) -> dict[str, Any]:
    global LAST_PUSH_STATUS
    webhook_url = get_sheets_webhook_url()
    if not webhook_url:
        LAST_PUSH_STATUS = {
            "ok": False,
            "message": "Webhook Google Apps Script non configuré",
            "at": _now_quebec_str(),
        }
        return LAST_PUSH_STATUS
    ok, msg = await asyncio.to_thread(_push_sheets_sync, webhook_url, payload)
    LAST_PUSH_STATUS = {
        "ok": ok,
        "message": msg,
        "at": _now_quebec_str(),
    }
    return LAST_PUSH_STATUS


# --------------------------------------------------------------------------- #
# Questions
# --------------------------------------------------------------------------- #
def load_questions() -> list[dict[str, Any]]:
    with QUESTIONS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    valid = []
    for item in data:
        options = item.get("options")
        answer = item.get("answer")
        if (
            isinstance(item.get("q"), str)
            and isinstance(options, list)
            and len(options) == 4
            and isinstance(answer, int)
            and 0 <= answer < 4
        ):
            diff = str(item.get("difficulty", "standard")).strip().lower()
            valid.append(
                {
                    "category": str(item.get("category", "Général")),
                    "difficulty": "debutant" if diff == "debutant" else "standard",
                    "q": item["q"],
                    "options": [str(o) for o in options],
                    "answer": answer,
                }
            )
    if not valid:
        raise RuntimeError("questions.json ne contient aucune question valide.")
    return valid


SYS_RNG = random.SystemRandom()
MIX_CATEGORY = "Mix Aléatoire"

QUESTIONS = load_questions()
BASE_CATEGORIES: list[str] = list(dict.fromkeys(q["category"] for q in QUESTIONS))
CATEGORIES: list[str] = BASE_CATEGORIES + [MIX_CATEGORY]
QUESTIONS_BY_CATEGORY: dict[str, list[dict[str, Any]]] = {
    cat: [q for q in QUESTIONS if q["category"] == cat] for cat in BASE_CATEGORIES
}
QUESTIONS_BY_CATEGORY[MIX_CATEGORY] = list(QUESTIONS)
QUESTIONS_BY_CAT_DIFF: dict[tuple[str, str], list[dict[str, Any]]] = {
    (cat, diff): [
        q for q in QUESTIONS if q["category"] == cat and q["difficulty"] == diff
    ]
    for cat in BASE_CATEGORIES
    for diff in ("standard", "debutant")
}
for _diff in ("standard", "debutant"):
    QUESTIONS_BY_CAT_DIFF[(MIX_CATEGORY, _diff)] = [
        q for q in QUESTIONS if q["difficulty"] == _diff
    ]


# --------------------------------------------------------------------------- #
# Modèle de jeu
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self, player_id: str):
        self.id = player_id
        self.name = ""
        self.avatar = "🙂"
        self.score = 0
        self.ws: WebSocket | None = None
        self.answer: int | None = None
        self.answer_remaining = 0.0
        self.last_points = 0
        self.last_correct: bool | None = None
        self.vote_category: str | None = None
        self.beginner = False
        self.correct_streak = 0
        self.wrong_streak = 0
        self.best_streak = 0
        self.fastest_time: float | None = None
        self.badges: list[dict[str, str]] = []
        self.disconnected_at: float | None = None
        self.missed_streak = 0

    @property
    def joined(self) -> bool:
        return bool(self.name)

    def reset_round(self) -> None:
        self.answer = None
        self.answer_remaining = 0.0

    def reset_stats(self) -> None:
        self.score = 0
        self.last_points = 0
        self.last_correct = None
        self.correct_streak = 0
        self.wrong_streak = 0
        self.best_streak = 0
        self.fastest_time = None
        self.badges = []
        self.missed_streak = 0
        self.reset_round()

    def award_badge(self, kind: str, icon: str, label: str) -> None:
        if not any(b["kind"] == kind for b in self.badges):
            self.badges.append({"kind": kind, "icon": icon, "label": label})


class Room:
    def __init__(self, code: str):
        self.code = code
        self.phase = "LOBBY"
        self.duration = LOBBY_DURATION
        self.timer = LOBBY_DURATION
        self.speed = 1
        self.players: dict[str, Player] = {}
        self.tvs: set[WebSocket] = set()
        self.cat_index = 0
        self.voting_enabled = False
        self.played_categories: set[str] = set()
        self.last_game_players: set[str] = set()
        self.active_difficulty = "standard"
        self.seen_questions: set[str] = set()
        self.questions: list[dict[str, Any]] = []
        self.q_index = -1
        self.counts = [0, 0, 0, 0]
        self.choice_players: list[list[dict[str, Any]]] = [[], [], [], []]
        self.fastest_record: dict[str, Any] | None = None
        self.round_events: list[dict[str, Any]] = []
        self.pending_kicks: list[tuple[WebSocket, str]] = []
        self.game_had_players = False
        self.session_ip: str = ""
        self.current_game_id: str = ""
        self.game_started_at: float | None = None
        self.game_ended_at: float | None = None
        self.game_timestamp_str: str = ""
        self.game_player_stats: dict[str, dict[str, Any]] = {}
        self.game_shares_count: int = 0
        self.game_logged: bool = False
        self.task: asyncio.Task | None = None
        self.last_tick = time.monotonic()
        self.empty_since: float | None = None

    @property
    def category(self) -> str:
        return CATEGORIES[self.cat_index % len(CATEGORIES)]

    @property
    def next_category(self) -> str:
        avail = self.available_categories()
        if self.voting_enabled:
            counts = self.vote_counts()
            top = self._leading_voted_category(avail, counts)
            if top is not None:
                return top
        for offset in range(1, len(CATEGORIES) + 1):
            candidate = CATEGORIES[(self.cat_index + offset) % len(CATEGORIES)]
            if candidate in avail:
                return candidate
        return CATEGORIES[(self.cat_index + 1) % len(CATEGORIES)]

    # ----- connexions ----------------------------------------------------- #
    def has_connections(self) -> bool:
        return bool(self.tvs) or any(p.ws is not None for p in self.players.values())

    def ensure_loop(self) -> None:
        self.empty_since = None
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(game_clock_loop(self))

    # ----- helpers --------------------------------------------------------- #
    def joined_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.joined]

    def leaderboard(self) -> list[Player]:
        return sorted(self.joined_players(), key=lambda p: (-p.score, p.name.lower()))

    def current_question(self) -> dict[str, Any] | None:
        if 0 <= self.q_index < len(self.questions):
            return self.questions[self.q_index]
        return None

    def remaining_precise(self) -> float:
        elapsed = (time.monotonic() - self.last_tick) * self.speed
        return max(0.0, min(float(QUESTION_DURATION), self.timer - elapsed))

    def all_answered(self) -> bool:
        active = [p for p in self.joined_players() if p.ws is not None]
        return bool(active) and all(p.answer is not None for p in active)

    def available_categories(self) -> list[str]:
        avail = [c for c in CATEGORIES if c not in self.played_categories]
        return avail if avail else list(CATEGORIES)

    def vote_counts(self) -> dict[str, int]:
        avail = set(self.available_categories())
        counts = {c: 0 for c in CATEGORIES}
        for p in self.joined_players():
            if p.ws is not None and p.vote_category in avail:
                counts[p.vote_category] += 1
        return counts

    def _leading_voted_category(
        self, avail: list[str], counts: dict[str, int]
    ) -> str | None:
        best_cat: str | None = None
        best_count = 0
        for c in avail:
            cnt = counts.get(c, 0)
            if cnt > best_count:
                best_count = cnt
                best_cat = c
        return best_cat

    def beginner_stats(self) -> tuple[int, int, bool]:
        active = [p for p in self.joined_players() if p.ws is not None]
        total = len(active)
        beginners = sum(1 for p in active if p.beginner)
        is_beginner = total > 0 and (beginners / total) > 0.5
        return beginners, total, is_beginner

    def refresh_category_locks(self) -> None:
        if self.phase not in ("LOBBY", "PODIUM"):
            return
        if self.last_game_players:
            active_ids = {p.id for p in self.joined_players() if p.ws is not None}
            not_replaying = len(self.last_game_players - active_ids)
            if not_replaying >= len(self.last_game_players) / 2:
                self.played_categories.clear()
                self.last_game_players.clear()
        if len(self.played_categories) >= len(CATEGORIES):
            self.played_categories.clear()

    def apply_votes(self) -> None:
        if self.phase != "LOBBY":
            return
        avail = self.available_categories()
        if self.voting_enabled:
            counts = self.vote_counts()
            best = self._leading_voted_category(avail, counts)
            if best is not None:
                cur_votes = counts.get(self.category, 0) if self.category in avail else -1
                if cur_votes < counts[best] or self.category not in avail:
                    self.cat_index = CATEGORIES.index(best)
                return
        if self.category not in avail:
            self.cat_index = CATEGORIES.index(avail[0])

    # ----- transitions ----------------------------------------------------- #
    def set_phase(self, phase: str, duration: int) -> None:
        self.phase = phase
        self.duration = duration
        self.timer = duration
        self.last_tick = time.monotonic()

    def _draw_questions(self, pool: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
        unseen = [q for q in pool if q["q"] not in self.seen_questions]
        seen = [q for q in pool if q["q"] in self.seen_questions]
        SYS_RNG.shuffle(unseen)
        if len(unseen) >= k:
            chosen = unseen[:k]
        else:
            chosen = list(unseen)
            pool_keys = {q["q"] for q in pool}
            self.seen_questions.difference_update(pool_keys)
            SYS_RNG.shuffle(seen)
            chosen.extend(seen[: k - len(chosen)])
        self.seen_questions.update(q["q"] for q in chosen)
        SYS_RNG.shuffle(chosen)

        result = []
        for q in chosen:
            indexed = list(enumerate(q["options"]))
            SYS_RNG.shuffle(indexed)
            new_options = [opt for _, opt in indexed]
            new_answer = next(
                i for i, (orig_i, _) in enumerate(indexed) if orig_i == q["answer"]
            )
            result.append(
                {
                    "category": q["category"],
                    "difficulty": q["difficulty"],
                    "q": q["q"],
                    "options": new_options,
                    "answer": new_answer,
                }
            )
        return result

    def register_player_in_game(self, p: Player) -> None:
        if self.phase not in ("QUESTION", "REVEAL", "INTER_PODIUM") or not p.joined:
            return
        entry = self.game_player_stats.get(p.id)
        if entry is None:
            self.game_player_stats[p.id] = {
                "name": p.name,
                "joined_at": time.monotonic(),
                "ended_at": None,
                "questions_answered": 0,
                "shares": 0,
            }
        else:
            entry["name"] = p.name
            entry["ended_at"] = None

    def mark_player_left_game(self, pid: str) -> None:
        if self.phase not in ("QUESTION", "REVEAL", "INTER_PODIUM"):
            return
        entry = self.game_player_stats.get(pid)
        if entry is not None and entry["ended_at"] is None:
            entry["ended_at"] = time.monotonic()

    def build_game_stats_payload(self) -> dict[str, Any] | None:
        if not self.current_game_id or not self.game_player_stats:
            return None
        stats_list = list(self.game_player_stats.values())
        end_mono = self.game_ended_at or time.monotonic()
        total_q = len(self.questions) or QUESTIONS_PER_GAME
        avg_q = round(
            sum(int(s["questions_answered"]) for s in stats_list) / len(stats_list), 1
        )
        detail_q = ", ".join(
            f"{s['name']}: {s['questions_answered']}/{total_q}" for s in stats_list
        )
        durs = [
            max(1, round((s["ended_at"] or end_mono) - float(s["joined_at"])))
            for s in stats_list
        ]
        avg_t = _format_duration(sum(durs) / len(durs))
        detail_t = ", ".join(
            f"{s['name']}: {_format_duration(d)}" for s, d in zip(stats_list, durs)
        )
        ts = self.game_timestamp_str or _now_quebec_str()
        row = [
            ts,
            self.session_ip or "Inconnue",
            len(stats_list),
            f"{avg_q:g} moy. ({detail_q})",
            f"{avg_t} moy. ({detail_t})",
            self.game_shares_count,
        ]
        return {
            "game_id": self.current_game_id,
            "sheet_id": GOOGLE_SHEET_ID,
            "category": self.category,
            "difficulty": self.active_difficulty,
            "row": row,
        }

    def trigger_stats_push(self) -> dict[str, Any] | None:
        payload = self.build_game_stats_payload()
        if payload is None:
            return None
        _record_game_history(payload)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return payload
        asyncio.create_task(push_game_stats_async(payload))
        return payload

    def finalize_and_push_stats(self) -> dict[str, Any] | None:
        if not self.current_game_id or not self.game_player_stats:
            return None
        if self.game_ended_at is None:
            self.game_ended_at = time.monotonic()
        if not self.game_timestamp_str:
            self.game_timestamp_str = _now_quebec_str()
        for stat in self.game_player_stats.values():
            if stat["ended_at"] is None:
                stat["ended_at"] = self.game_ended_at
        self.game_logged = True
        return self.trigger_stats_push()

    def start_game(self) -> None:
        self.refresh_category_locks()
        self.apply_votes()
        _, _, is_beginner = self.beginner_stats()
        self.active_difficulty = "debutant" if is_beginner else "standard"
        pool = (
            QUESTIONS_BY_CAT_DIFF.get((self.category, self.active_difficulty))
            or QUESTIONS_BY_CATEGORY.get(self.category, QUESTIONS)
        )
        self.questions = self._draw_questions(pool, min(QUESTIONS_PER_GAME, len(pool)))
        self.q_index = 0
        self.fastest_record = None
        self.round_events = []
        self.played_categories.add(self.category)
        self.last_game_players = {p.id for p in self.joined_players() if p.ws is not None}
        self.game_had_players = bool(self.joined_players())
        now_mono = time.monotonic()
        self.current_game_id = f"{self.code}-{int(time.time() * 1000)}"
        self.game_started_at = now_mono
        self.game_ended_at = None
        self.game_timestamp_str = ""
        self.game_shares_count = 0
        self.game_logged = False
        self.game_player_stats = {
            p.id: {
                "name": p.name,
                "joined_at": now_mono,
                "ended_at": None,
                "questions_answered": 0,
                "shares": 0,
            }
            for p in self.joined_players()
        }
        for p in self.players.values():
            p.reset_stats()
            p.vote_category = None
        self.set_phase("QUESTION", QUESTION_DURATION)

    def kick_player(self, pid: str, reason: str) -> bool:
        p = self.players.get(pid)
        if p is None:
            return False
        self.mark_player_left_game(pid)
        if p.ws is not None:
            self.pending_kicks.append((p.ws, reason))
            p.name = ""
            p.vote_category = None
            p.reset_stats()
        else:
            self.players.pop(pid, None)
        self.refresh_category_locks()
        self.apply_votes()
        self.check_empty_active_game()
        return True

    def check_empty_active_game(self) -> None:
        if self.phase not in ("QUESTION", "REVEAL", "INTER_PODIUM"):
            return
        if self.game_had_players and not self.joined_players():
            self.back_to_lobby()
        elif self.phase == "QUESTION" and self.all_answered():
            self.reveal()

    def prune_inactive_players(self) -> bool:
        now = time.monotonic()
        changed = False
        grace = (
            DISCONNECT_GRACE_LOBBY
            if self.phase in ("LOBBY", "PODIUM")
            else DISCONNECT_GRACE_GAME
        )
        for pid, p in list(self.players.items()):
            if p.ws is None:
                if not p.joined:
                    self.players.pop(pid, None)
                    changed = True
                elif p.disconnected_at is not None and (now - p.disconnected_at) >= grace:
                    self.mark_player_left_game(pid)
                    self.players.pop(pid, None)
                    changed = True
        if changed:
            self.refresh_category_locks()
            self.apply_votes()
            self.check_empty_active_game()
        return changed

    def select_category(
        self,
        category: str | None = None,
        index: Any = None,
        enforce_available: bool = False,
    ) -> bool:
        target_idx = None
        if isinstance(category, str) and category in QUESTIONS_BY_CATEGORY:
            target_idx = CATEGORIES.index(category)
        elif index is not None:
            try:
                idx = int(index)
                if 0 <= idx < len(CATEGORIES):
                    target_idx = idx
            except (TypeError, ValueError):
                pass
        if target_idx is None:
            return False
        if enforce_available and CATEGORIES[target_idx] not in self.available_categories():
            return False
        self.cat_index = target_idx
        return True

    def _make_event(
        self,
        kind: str,
        title: str,
        subtitle: str,
        icon: str,
        p: Player,
        extra: str = "",
    ) -> dict[str, Any]:
        return {
            "kind": kind,
            "title": title,
            "subtitle": subtitle,
            "icon": icon,
            "player_id": p.id,
            "name": p.name,
            "avatar": p.avatar,
            "extra": extra,
        }

    def best_streak_record(self) -> dict[str, Any] | None:
        best = max(
            (p for p in self.joined_players() if p.best_streak >= 2),
            key=lambda p: (p.best_streak, p.score, p.name.lower()),
            default=None,
        )
        if best is None:
            return None
        return {
            "id": best.id,
            "name": best.name,
            "avatar": best.avatar,
            "streak": best.best_streak,
        }

    def reveal(self) -> None:
        q = self.current_question()
        prev_board = self.leaderboard()
        prev_ranks = {p.id: idx + 1 for idx, p in enumerate(prev_board)}
        prev_leader_id = prev_board[0].id if prev_board and prev_board[0].score > 0 else None

        self.counts = [0, 0, 0, 0]
        self.choice_players = [[], [], [], []]
        self.round_events = []
        correct_players: list[tuple[Player, float]] = []
        answered_count = 0

        for p in sorted(self.joined_players(), key=lambda x: x.name.lower()):
            if p.answer is not None and 0 <= p.answer < 4:
                answered_count += 1
                p.missed_streak = 0
                if p.id not in self.game_player_stats:
                    self.register_player_in_game(p)
                if p.id in self.game_player_stats:
                    self.game_player_stats[p.id]["questions_answered"] += 1
                self.counts[p.answer] += 1
                self.choice_players[p.answer].append(
                    {"id": p.id, "name": p.name, "avatar": p.avatar}
                )
            else:
                p.missed_streak += 1
            if q is not None and p.answer == q["answer"]:
                ratio = p.answer_remaining / QUESTION_DURATION
                pts = round(MIN_POINTS + (MAX_POINTS - MIN_POINTS) * ratio)
                pts = max(MIN_POINTS, min(MAX_POINTS, pts))
                elapsed = round(max(0.05, QUESTION_DURATION - p.answer_remaining), 2)
                p.score += pts
                p.last_points = pts
                p.last_correct = True
                p.correct_streak += 1
                p.wrong_streak = 0
                p.best_streak = max(p.best_streak, p.correct_streak)
                if p.fastest_time is None or elapsed < p.fastest_time:
                    p.fastest_time = elapsed
                correct_players.append((p, elapsed))

                if p.correct_streak == 15:
                    p.award_badge("STREAK_15", "👑", "15 à la suite")
                    self.round_events.append(
                        self._make_event(
                            "STREAK_15",
                            "SANS-FAUTE ABSOLU !",
                            "15 bonnes réponses d'affilée !",
                            "👑",
                            p,
                            "15/15",
                        )
                    )
                elif p.correct_streak == 10:
                    p.award_badge("STREAK_10", "⚡", "10 à la suite")
                    self.round_events.append(
                        self._make_event(
                            "STREAK_10",
                            "SÉRIE LÉGENDAIRE !",
                            "10 bonnes réponses à la suite !",
                            "⚡",
                            p,
                            "10🔥",
                        )
                    )
                elif p.correct_streak == 5:
                    p.award_badge("STREAK_5", "🔥", "5 à la suite")
                    self.round_events.append(
                        self._make_event(
                            "STREAK_5",
                            "SÉRIE EN FEU !",
                            "5 bonnes réponses à la suite !",
                            "🔥",
                            p,
                            "5🔥",
                        )
                    )
                if p.answer_remaining <= 2.0:
                    p.award_badge("BUZZER_BEATER", "🍀", "Buzzer beater")
                    self.round_events.append(
                        self._make_event(
                            "BUZZER_BEATER",
                            "SAUVÉ PAR LE GONG !",
                            f"Réponse juste à la dernière seconde ({elapsed:.2f}s) !",
                            "🍀",
                            p,
                        )
                    )
            else:
                p.last_points = 0
                p.last_correct = False
                if p.answer is not None or p.ws is not None:
                    p.wrong_streak += 1
                    p.correct_streak = 0
                    if p.wrong_streak >= 5 and p.wrong_streak % 5 == 0:
                        p.award_badge("WRONG_5", "🧊", "5 faux d'affilée")
                        self.round_events.append(
                            self._make_event(
                                "WRONG_5",
                                "COUP DE FROID !",
                                f"{p.wrong_streak} mauvaises réponses à la suite… courage !",
                                "🧊",
                                p,
                                f"{p.wrong_streak}❄️",
                            )
                        )

        if correct_players:
            correct_players.sort(key=lambda item: (item[1], item[0].name.lower()))
            fastest_p, fastest_t = correct_players[0]
            if self.fastest_record is None or fastest_t < self.fastest_record["time"]:
                self.fastest_record = {
                    "id": fastest_p.id,
                    "name": fastest_p.name,
                    "avatar": fastest_p.avatar,
                    "time": fastest_t,
                    "q_index": self.q_index,
                }
                fastest_p.award_badge("FASTEST_RECORD", "🚀", f"Éclair {fastest_t:.2f}s")
                self.round_events.append(
                    self._make_event(
                        "FASTEST_RECORD",
                        "RECORD DE VITESSE !",
                        f"Réponse juste la plus rapide de la partie ({fastest_t:.2f}s) !",
                        "🚀",
                        fastest_p,
                        f"{fastest_t:.2f}s",
                    )
                )
            elif len(correct_players) >= 2:
                self.round_events.append(
                    self._make_event(
                        "FIRST_BLOOD",
                        "DÉGAINÉ EN PREMIER !",
                        f"Plus rapide sur cette question ({fastest_t:.2f}s)",
                        "🎯",
                        fastest_p,
                        f"{fastest_t:.2f}s",
                    )
                )

        if answered_count >= 2 and len(correct_players) == 1:
            lone = correct_players[0][0]
            lone.award_badge("LONE_WOLF", "💀", "Seul contre tous")
            self.round_events.append(
                self._make_event(
                    "LONE_WOLF",
                    "SEUL CONTRE TOUS !",
                    "Unique bonne réponse de la salle !",
                    "💀",
                    lone,
                )
            )

        if self.q_index > 0:
            new_board = self.leaderboard()
            if new_board and new_board[0].score > 0 and prev_leader_id and new_board[0].id != prev_leader_id:
                leader = new_board[0]
                leader.award_badge("NEW_LEADER", "🥇", "Prise de pouvoir")
                self.round_events.append(
                    self._make_event(
                        "NEW_LEADER",
                        "NOUVEAU LEADER !",
                        "Prend la 1re place du classement !",
                        "🥇",
                        leader,
                    )
                )
            for idx, p in enumerate(new_board):
                old_rank = prev_ranks.get(p.id)
                if old_rank and (old_rank - (idx + 1)) >= 3 and p.last_correct:
                    gain = old_rank - (idx + 1)
                    p.award_badge("COMEBACK", "🦅", "Remontada")
                    self.round_events.append(
                        self._make_event(
                            "COMEBACK",
                            "REMONTADA !",
                            f"+{gain} places gagnées sur ce tour !",
                            "🦅",
                            p,
                        )
                    )

        priority = {
            "STREAK_15": 0,
            "STREAK_10": 1,
            "FASTEST_RECORD": 2,
            "STREAK_5": 3,
            "LONE_WOLF": 4,
            "WRONG_5": 5,
            "NEW_LEADER": 6,
            "COMEBACK": 7,
            "BUZZER_BEATER": 8,
            "FIRST_BLOOD": 9,
        }
        self.round_events.sort(key=lambda ev: priority.get(ev["kind"], 99))
        self.set_phase("REVEAL", REVEAL_DURATION)

        afk_ids = [
            pid
            for pid, p in list(self.players.items())
            if p.joined and p.missed_streak >= AFK_MISSED_LIMIT
        ]
        for pid in afk_ids:
            self.kick_player(
                pid,
                f"Tu as été retiré de la partie après {AFK_MISSED_LIMIT} questions sans réponse.",
            )

    def back_to_lobby(self) -> None:
        if not self.game_logged and self.game_player_stats:
            self.finalize_and_push_stats()
        self.questions = []
        self.q_index = -1
        self.fastest_record = None
        self.round_events = []
        self.game_had_players = False
        self.current_game_id = ""
        self.game_started_at = None
        self.game_ended_at = None
        self.game_timestamp_str = ""
        self.game_player_stats = {}
        self.game_shares_count = 0
        self.game_logged = False
        for pid, p in list(self.players.items()):
            if p.ws is None:
                self.players.pop(pid, None)
            else:
                p.reset_stats()
        self.set_phase("LOBBY", LOBBY_DURATION)
        self.refresh_category_locks()
        self.apply_votes()

    def advance(self) -> None:
        """Passe à l'étape suivante (fin de timer ou commande régie NEXT)."""
        if self.phase == "LOBBY":
            self.start_game()
        elif self.phase == "QUESTION":
            self.reveal()
        elif self.phase == "REVEAL":
            next_q = self.q_index + 1
            if next_q < len(self.questions):
                if next_q % CHECKPOINT_INTERVAL == 0:
                    self.set_phase("INTER_PODIUM", INTER_PODIUM_DURATION)
                else:
                    self.q_index = next_q
                    for p in self.players.values():
                        p.reset_round()
                    self.set_phase("QUESTION", QUESTION_DURATION)
            else:
                self.set_phase("PODIUM", PODIUM_DURATION)
                self.finalize_and_push_stats()
        elif self.phase == "INTER_PODIUM":
            self.q_index += 1
            for p in self.players.values():
                p.reset_round()
            self.set_phase("QUESTION", QUESTION_DURATION)
        else:  # PODIUM -> catégorie suivante & nouveau cycle
            self.refresh_category_locks()
            self.cat_index = CATEGORIES.index(self.next_category)
            self.back_to_lobby()

    def on_timer_expired(self) -> None:
        if self.phase == "LOBBY" and not self.joined_players():
            # Personne n'est inscrit : on relance l'attente.
            self.set_phase("LOBBY", LOBBY_DURATION)
        else:
            self.advance()

    # ----- états envoyés aux clients --------------------------------------- #
    def _public_player(self, p: Player) -> dict[str, Any]:
        return {
            "id": p.id,
            "name": p.name,
            "avatar": p.avatar,
            "score": p.score,
            "connected": p.ws is not None,
            "answered": p.answer is not None,
            "last_points": p.last_points,
            "last_correct": p.last_correct,
            "beginner": p.beginner,
            "vote_category": p.vote_category,
            "correct_streak": p.correct_streak,
            "wrong_streak": p.wrong_streak,
            "best_streak": p.best_streak,
            "fastest_time": p.fastest_time,
            "badges": p.badges,
            "missed_streak": p.missed_streak,
        }

    def _question_payload(self, with_answer: bool) -> dict[str, Any] | None:
        q = self.current_question()
        if q is None or self.phase not in ("QUESTION", "REVEAL"):
            return None
        payload = {"category": q["category"], "q": q["q"], "options": q["options"]}
        if with_answer:
            payload["answer"] = q["answer"]
        return payload

    def tv_state(self) -> dict[str, Any]:
        joined = self.joined_players()
        reveal = self.phase == "REVEAL"
        b_count, b_total, is_beginner = self.beginner_stats()
        diff = (
            ("debutant" if is_beginner else "standard")
            if self.phase in ("LOBBY", "PODIUM")
            else self.active_difficulty
        )
        return {
            "type": "state",
            "room": self.code,
            "phase": self.phase,
            "timer": max(0, int(self.timer)),
            "duration": self.duration,
            "speed": self.speed,
            "voting_enabled": self.voting_enabled,
            "categories": CATEGORIES,
            "available_categories": self.available_categories(),
            "played_categories": [c for c in CATEGORIES if c in self.played_categories],
            "votes": self.vote_counts(),
            "beginner_count": b_count,
            "beginner_total": b_total,
            "beginner_mode": is_beginner,
            "difficulty": diff,
            "category": self.category,
            "cat_index": self.cat_index,
            "next_category": self.next_category,
            "q_index": self.q_index,
            "q_total": len(self.questions),
            "question": self._question_payload(with_answer=reveal),
            "counts": self.counts if reveal else None,
            "choice_players": self.choice_players if reveal else None,
            "round_events": self.round_events if reveal else [],
            "fastest_record": self.fastest_record,
            "best_streak_record": self.best_streak_record(),
            "answered_count": sum(1 for p in joined if p.answer is not None),
            "players": [self._public_player(p) for p in joined],
            "leaderboard": [self._public_player(p) for p in self.leaderboard()],
            "sheets_webhook_set": bool(get_sheets_webhook_url()),
            "sheets_url": GOOGLE_SHEET_URL,
            "last_push_status": LAST_PUSH_STATUS,
            "page_lock_enabled": bool(AUTH_STATE.get("page_lock_enabled", True)),
        }

    def player_state(self, p: Player) -> dict[str, Any]:
        board = self.leaderboard()
        rank = next((i + 1 for i, x in enumerate(board) if x.id == p.id), None)
        reveal = self.phase == "REVEAL"
        b_count, b_total, is_beginner = self.beginner_stats()
        diff = (
            ("debutant" if is_beginner else "standard")
            if self.phase in ("LOBBY", "PODIUM")
            else self.active_difficulty
        )
        return {
            "type": "state",
            "room": self.code,
            "phase": self.phase,
            "timer": max(0, int(self.timer)),
            "duration": self.duration,
            "voting_enabled": self.voting_enabled,
            "categories": CATEGORIES,
            "available_categories": self.available_categories(),
            "played_categories": [c for c in CATEGORIES if c in self.played_categories],
            "votes": self.vote_counts(),
            "beginner_count": b_count,
            "beginner_total": b_total,
            "beginner_mode": is_beginner,
            "difficulty": diff,
            "category": self.category,
            "cat_index": self.cat_index,
            "next_category": self.next_category,
            "q_index": self.q_index,
            "q_total": len(self.questions),
            "question": self._question_payload(with_answer=reveal),
            "joined": p.joined,
            "name": p.name,
            "avatar": p.avatar,
            "score": p.score,
            "rank": rank,
            "players_count": len(board),
            "my_answer": p.answer,
            "my_vote": p.vote_category,
            "my_beginner": p.beginner,
            "my_correct_streak": p.correct_streak,
            "my_wrong_streak": p.wrong_streak,
            "my_best_streak": p.best_streak,
            "my_fastest_time": p.fastest_time,
            "my_badges": p.badges,
            "round_events": self.round_events if reveal else [],
            "fastest_record": self.fastest_record,
            "best_streak_record": self.best_streak_record(),
            "last_points": p.last_points,
            "last_correct": p.last_correct,
            "top3": [self._public_player(x) for x in board[:3]],
        }

    async def broadcast(self) -> None:
        tasks = []
        kicks = self.pending_kicks
        self.pending_kicks = []
        for ws, reason in kicks:
            tasks.append(safe_send(ws, {"type": "kicked", "reason": reason}))
        tv = self.tv_state()
        for ws in list(self.tvs):
            tasks.append(safe_send(ws, tv))
        for p in list(self.players.values()):
            if p.ws is not None:
                tasks.append(safe_send(p.ws, self.player_state(p)))
        if tasks:
            await asyncio.gather(*tasks)


rooms: dict[str, Room] = {}


async def safe_send(ws: WebSocket, data: dict[str, Any]) -> bool:
    try:
        await ws.send_text(json.dumps(data, ensure_ascii=False))
        return True
    except Exception:
        return False


def normalize_code(code: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())[:CODE_LENGTH]


def generate_code() -> str:
    while True:
        code = "".join(random.choices(CODE_ALPHABET, k=CODE_LENGTH))
        if code not in rooms:
            return code


def cleanup_rooms() -> None:
    now = time.monotonic()
    for code, room in list(rooms.items()):
        if (
            not room.has_connections()
            and room.empty_since is not None
            and now - room.empty_since > ROOM_TTL
        ):
            del rooms[code]


async def game_clock_loop(room: Room) -> None:
    """Horloge de la salle : un tick par seconde (÷ vitesse) tant qu'il y a du monde."""
    try:
        while room.has_connections():
            await asyncio.sleep(1 / room.speed)
            room.prune_inactive_players()
            room.timer -= 1
            room.last_tick = time.monotonic()
            if room.timer <= 0:
                room.on_timer_expired()
            await room.broadcast()
    finally:
        room.empty_since = time.monotonic()


# --------------------------------------------------------------------------- #
# Photobooth Simple Booth (Proxy + Cache)
# --------------------------------------------------------------------------- #
def extract_gallery_code(url_or_code: str) -> str | None:
    raw = (url_or_code or "").strip()
    m = re.search(r"simplebooth\.com/gallery/([a-zA-Z0-9]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9]{6,24}", raw):
        return raw
    return None


def _http_get_text(url: str, accept: str = "application/hal+json, application/json") -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": accept,
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _parse_created_at(val: Any) -> tuple[int, str]:
    if isinstance(val, (int, float)):
        ts_ms = int(val if val > 1e12 else val * 1000)
        iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        return ts_ms, iso
    if isinstance(val, str) and val.strip():
        s = val.strip()
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000), s
        except ValueError:
            pass
    return 0, ""


def _normalize_upload(u: Any) -> dict[str, Any] | None:
    if not isinstance(u, dict):
        return None
    if u.get("deleted") is True:
        return None
    if u.get("processed") is False:
        return None
    code = str(u.get("code") or "").strip()
    if not code:
        return None
    media = u.get("media") if isinstance(u.get("media"), dict) else {}
    original = str(media.get("original") or media.get("medium") or "").strip()
    if not original:
        return None
    mime = str(u.get("mimeType") or "").lower()
    if mime.startswith("video/") or original.lower().endswith((".mp4", ".webm", ".mov")):
        return None
    thumb = str(media.get("medium") or media.get("thumbnail") or original).strip()
    ts_ms, created_iso = _parse_created_at(u.get("createdAt"))
    return {
        "code": code,
        "url": original,
        "thumb": thumb,
        "createdAt": created_iso,
        "ts": ts_ms,
        "width": u.get("width"),
        "height": u.get("height"),
        "mimeType": mime or "image/jpeg",
    }


def _extract_window_simplebooth(html: str) -> list[dict[str, Any]]:
    m = re.search(r"window\.simpleBooth\s*=\s*", html)
    if not m:
        return []
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[m.end() :])
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    for candidate in (
        obj.get("uploads"),
        (obj.get("_embedded") or {}).get("uploads") if isinstance(obj.get("_embedded"), dict) else None,
        (obj.get("gallery") or {}).get("uploads") if isinstance(obj.get("gallery"), dict) else None,
    ):
        if isinstance(candidate, list):
            return candidate
    return []


def _fetch_simplebooth_sync(gallery_code: str, limit: int = 50) -> list[dict[str, Any]]:
    photos_by_code: dict[str, dict[str, Any]] = {}
    next_url: str | None = (
        f"https://api.simplebooth.com/api/uploads?gallery={gallery_code}"
        f"&order-by=created-at&order=desc&page-size={limit}"
    )

    pages = 0
    while next_url and len(photos_by_code) < limit and pages < 4:
        pages += 1
        try:
            raw = _http_get_text(next_url)
            data = json.loads(raw)
        except Exception:
            break
        uploads = ((data.get("_embedded") or {}).get("uploads") or []) if isinstance(data, dict) else []
        for item in uploads:
            norm = _normalize_upload(item)
            if norm and norm["code"] not in photos_by_code:
                photos_by_code[norm["code"]] = norm
                if len(photos_by_code) >= limit:
                    break
        nxt = ((data.get("_links") or {}).get("next") or {}) if isinstance(data, dict) else {}
        href = nxt.get("href") if isinstance(nxt, dict) else None
        next_url = urljoin(next_url, href) if href else None

    if not photos_by_code:
        try:
            html = _http_get_text(
                f"https://www.simplebooth.com/gallery/{gallery_code}",
                accept="text/html,application/xhtml+xml",
            )
            for item in _extract_window_simplebooth(html):
                norm = _normalize_upload(item)
                if norm and norm["code"] not in photos_by_code:
                    photos_by_code[norm["code"]] = norm
        except Exception:
            pass

    photos = sorted(photos_by_code.values(), key=lambda x: x["ts"], reverse=True)
    return photos[:limit]


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
app = FastAPI(title="Laser Quiz")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "rooms": len(rooms), "questions": len(QUESTIONS)}


LOGO_REMOTE_URL = "https://lasergame-evolution.ca/wp-content/uploads/2025/01/CANADA-vector-logo-color-bl-black-copie-01.png"
_LOGO_CACHE: bytes | None = None


def _fetch_logo_bytes() -> bytes:
    req = urllib.request.Request(
        LOGO_REMOTE_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


@app.get("/api/logo")
async def api_logo() -> Response:
    global _LOGO_CACHE
    if _LOGO_CACHE is None:
        try:
            data = await asyncio.to_thread(_fetch_logo_bytes)
            if data:
                _LOGO_CACHE = data
        except Exception:
            pass
    if not _LOGO_CACHE:
        raise HTTPException(status_code=404, detail="Logo indisponible.")
    return Response(
        content=_LOGO_CACHE,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/api/photobooth")
async def api_photobooth(
    url: str,
    limit: int = 18,
    order: str = "asc",
    force: int = 0,
) -> dict[str, Any]:
    code = extract_gallery_code(url)
    if not code:
        raise HTTPException(status_code=400, detail="Lien de galerie Simple Booth invalide.")
    capped = max(1, min(limit, 50))
    now = time.monotonic()
    key = (code, capped)
    cached = PHOTOBOOTH_CACHE.get(key)
    if not force and cached and (now - cached[0]) < PHOTOBOOTH_CACHE_TTL:
        photos = list(cached[1])
    else:
        photos = await asyncio.to_thread(_fetch_simplebooth_sync, code, capped)
        if photos:
            PHOTOBOOTH_CACHE[key] = (now, list(photos))
    sort_order = "desc" if str(order).lower() == "desc" else "asc"
    ordered = sorted(photos, key=lambda x: x["ts"], reverse=(sort_order == "desc"))
    return {
        "ok": True,
        "galleryCode": code,
        "count": len(ordered),
        "order": sort_order,
        "photos": ordered,
    }


@app.get("/api/stats")
async def api_stats() -> dict[str, Any]:
    return {
        "ok": True,
        "sheet_id": GOOGLE_SHEET_ID,
        "sheet_url": GOOGLE_SHEET_URL,
        "webhook_configured": bool(get_sheets_webhook_url()),
        "last_push_status": LAST_PUSH_STATUS,
        "headers": STATS_HEADERS,
        "count": len(GAME_LOGS_HISTORY),
        "history": list(reversed(GAME_LOGS_HISTORY[-50:])),
    }


@app.post("/api/stats/config")
async def api_stats_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    raw_url = str(body.get("webhook_url") or "")
    try:
        saved = set_sheets_webhook_url(raw_url)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return {
        "ok": True,
        "webhook_configured": bool(saved or get_sheets_webhook_url()),
        "sheet_url": GOOGLE_SHEET_URL,
    }


@app.post("/api/stats/test")
async def api_stats_test() -> dict[str, Any]:
    test_payload = {
        "game_id": f"TEST-{int(time.time() * 1000)}",
        "sheet_id": GOOGLE_SHEET_ID,
        "category": "Test",
        "difficulty": "standard",
        "row": [
            _now_quebec_str(),
            "127.0.0.1",
            2,
            "14 moy. (Test1: 15/15, Test2: 13/15)",
            "3m 45s moy. (Test1: 4m 00s, Test2: 3m 30s)",
            1,
        ],
    }
    status = await push_game_stats_async(test_payload)
    return {"ok": bool(status.get("ok")), "status": status}


@app.get("/api/stats/csv")
async def api_stats_csv() -> Response:
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(STATS_HEADERS)
    for item in GAME_LOGS_HISTORY:
        writer.writerow(item.get("row") or [])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="laser-quiz-stats.csv"'},
    )


@app.get("/api/auth/status")
async def api_auth_status(user_token: str = "", admin_token: str = "") -> dict[str, Any]:
    user_payload = _verify_auth_token(user_token)
    admin_payload = _verify_auth_token(admin_token)
    admin_valid = bool(admin_payload and admin_payload.get("r") == "admin")
    return {
        "ok": True,
        "page_lock_enabled": bool(AUTH_STATE.get("page_lock_enabled", True)),
        "user_unlocked": bool(user_payload or admin_valid),
        "admin_authenticated": admin_valid,
        "admin_must_change_password": bool(
            (AUTH_STATE.get("users") or {}).get("AdminLGE", {}).get("must_change_password", False)
        ),
    }


@app.post("/api/auth/login")
async def api_auth_login(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    target = str(body.get("target") or "page").strip().lower()
    user = _find_user(username)
    if user is None:
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect.")
    if target == "regie" and user["username"] != "AdminLGE":
        raise HTTPException(status_code=403, detail="Seul AdminLGE peut accéder à la régie.")
    if not hmac.compare_digest(
        str(user.get("password_hash") or ""),
        _hash_password(password, str(user.get("salt") or "")),
    ):
        raise HTTPException(status_code=401, detail="Identifiant ou mot de passe incorrect.")

    if user.get("must_change_password"):
        return {
            "ok": True,
            "must_change_password": True,
            "username": user["username"],
            "role": user["role"],
            "change_token": _make_auth_token(
                user["username"], user["role"], purpose="pwd_change"
            ),
        }

    return {
        "ok": True,
        "must_change_password": False,
        "username": user["username"],
        "role": user["role"],
        "token": _make_auth_token(user["username"], user["role"], purpose="session"),
    }


@app.post("/api/auth/change-password")
async def api_auth_change_password(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    token = str(body.get("token") or "")
    new_password = str(body.get("new_password") or "")
    target_user = str(body.get("target_user") or "").strip()

    payload = _verify_auth_token(token, expected_purpose="pwd_change")
    is_admin_session = False
    if payload is None:
        payload = _verify_auth_token(token, expected_purpose="session")
        if payload is None or payload.get("r") != "admin":
            raise HTTPException(status_code=401, detail="Session expirée ou non autorisée.")
        is_admin_session = True

    if len(new_password.strip()) < 4:
        raise HTTPException(
            status_code=400,
            detail="Le mot de passe doit contenir au moins 4 caractères.",
        )

    target_name = target_user if (is_admin_session and target_user) else str(payload["u"])
    target = _find_user(target_name)
    if target is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")

    new_salt = secrets.token_hex(16)
    target["salt"] = new_salt
    target["password_hash"] = _hash_password(new_password, new_salt)
    target["must_change_password"] = False
    _save_auth_state(AUTH_STATE)

    return {
        "ok": True,
        "username": target["username"],
        "role": target["role"],
        "token": _make_auth_token(target["username"], target["role"], purpose="session"),
    }


@app.post("/api/auth/page-lock")
async def api_auth_page_lock(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    token = str(body.get("token") or "")
    payload = _verify_auth_token(token, expected_purpose="session")
    if payload is None or payload.get("r") != "admin":
        raise HTTPException(status_code=403, detail="Accès réservé à AdminLGE.")
    AUTH_STATE["page_lock_enabled"] = bool(body.get("enabled"))
    _save_auth_state(AUTH_STATE)
    for r in list(rooms.values()):
        await r.broadcast()
    return {
        "ok": True,
        "page_lock_enabled": bool(AUTH_STATE["page_lock_enabled"]),
    }


@app.websocket("/ws/tv")
async def ws_tv(websocket: WebSocket, room: str | None = None) -> None:
    await websocket.accept()
    cleanup_rooms()
    code = normalize_code(room)
    current = rooms.get(code) if code else None
    if current is None:
        # Reconnexion après redémarrage : on garde le même code s'il est valide.
        if len(code) != CODE_LENGTH or code in rooms:
            code = generate_code()
        current = Room(code)
        rooms[code] = current

    tv_ip = _extract_client_ip(websocket)
    if tv_ip and tv_ip != "Inconnue":
        current.session_ip = tv_ip

    current.tvs.add(websocket)
    current.ensure_loop()
    await safe_send(websocket, {"type": "room", "room": current.code})
    await current.broadcast()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            cmd = str(msg.get("type") or msg.get("cmd") or "").upper()
            if cmd == "NEXT":
                current.advance()
                await current.broadcast()
            elif cmd == "TOGGLE_SPEED":
                current.speed = 5 if current.speed == 1 else 1
                await current.broadcast()
            elif cmd == "TOGGLE_VOTE":
                current.voting_enabled = not current.voting_enabled
                current.refresh_category_locks()
                current.apply_votes()
                await current.broadcast()
            elif cmd == "SELECT_CATEGORY":
                if current.phase in ("LOBBY", "PODIUM") and current.select_category(
                    msg.get("category"),
                    msg.get("index"),
                    enforce_available=current.voting_enabled,
                ):
                    if current.phase == "PODIUM" and msg.get("start"):
                        current.start_game()
                    await current.broadcast()
            elif cmd == "START_NEXT_CATEGORY":
                if not current.game_logged and current.game_player_stats:
                    current.finalize_and_push_stats()
                current.refresh_category_locks()
                current.cat_index = CATEGORIES.index(current.next_category)
                if current.joined_players():
                    current.start_game()
                else:
                    current.back_to_lobby()
                await current.broadcast()
            elif cmd == "KICK_PLAYER":
                target_id = str(msg.get("player_id") or "")
                if current.kick_player(
                    target_id, "Tu as été retiré de la salle par la régie."
                ):
                    await current.broadcast()
            elif cmd == "TOGGLE_PAGE_LOCK":
                AUTH_STATE["page_lock_enabled"] = not bool(AUTH_STATE.get("page_lock_enabled", True))
                _save_auth_state(AUTH_STATE)
                for r in list(rooms.values()):
                    await r.broadcast()
            elif cmd == "SET_SHEETS_WEBHOOK":
                global LAST_PUSH_STATUS
                try:
                    saved = set_sheets_webhook_url(str(msg.get("webhook_url") or ""))
                    LAST_PUSH_STATUS = {
                        "ok": True if saved else None,
                        "message": "Webhook enregistré" if saved else "Webhook vidé (var env utilisée)",
                        "at": _now_quebec_str(),
                    }
                except ValueError as err:
                    LAST_PUSH_STATUS = {
                        "ok": False,
                        "message": str(err),
                        "at": _now_quebec_str(),
                    }
                await current.broadcast()
            elif cmd == "TEST_SHEETS_PUSH":
                test_payload = {
                    "game_id": f"TEST-{int(time.time() * 1000)}",
                    "sheet_id": GOOGLE_SHEET_ID,
                    "category": current.category,
                    "difficulty": current.active_difficulty,
                    "row": [
                        _now_quebec_str(),
                        current.session_ip or _extract_client_ip(websocket),
                        max(1, len(current.joined_players())),
                        "14 moy. (Test1: 15/15, Test2: 13/15)",
                        "3m 45s moy. (Test1: 4m 00s, Test2: 3m 30s)",
                        1,
                    ],
                }
                await push_game_stats_async(test_payload)
                await current.broadcast()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        current.tvs.discard(websocket)


@app.websocket("/ws/player/{room_code}/{player_id}")
async def ws_player(websocket: WebSocket, room_code: str, player_id: str) -> None:
    await websocket.accept()
    room = rooms.get(normalize_code(room_code))
    if room is None:
        await safe_send(websocket, {"type": "error", "message": "Salle introuvable. Vérifiez le code affiché sur la TV."})
        await websocket.close(code=4404)
        return

    if not room.session_ip:
        room.session_ip = _extract_client_ip(websocket)

    pid = re.sub(r"[^A-Za-z0-9_-]", "", player_id)[:64] or generate_code()
    player = room.players.get(pid)
    if player is None:
        player = Player(pid)
        room.players[pid] = player
    elif player.ws is not None and player.ws is not websocket:
        old = player.ws
        player.ws = None
        try:
            await old.close(code=4000)
        except Exception:
            pass

    player.ws = websocket
    player.disconnected_at = None
    if player.joined:
        room.register_player_in_game(player)
    room.ensure_loop()
    room.refresh_category_locks()
    room.apply_votes()
    await room.broadcast()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = str(msg.get("type", "")).upper()

            if kind == "JOIN":
                name = " ".join(str(msg.get("name", "")).split())[:MAX_NAME_LENGTH]
                avatar = str(msg.get("avatar", "🙂")).strip()[:8] or "🙂"
                if not name:
                    await safe_send(websocket, {"type": "error", "message": "Choisis un pseudo."})
                    continue
                player.name = name
                player.avatar = avatar
                player.disconnected_at = None
                player.missed_streak = 0
                if "beginner" in msg:
                    player.beginner = bool(msg.get("beginner"))
                if "vote_category" in msg or "category" in msg:
                    vote_cat = msg.get("vote_category", msg.get("category"))
                    if vote_cat in room.available_categories():
                        player.vote_category = vote_cat
                    elif vote_cat is None or vote_cat == "":
                        player.vote_category = None
                room.register_player_in_game(player)
                room.refresh_category_locks()
                room.apply_votes()
                await room.broadcast()

            elif kind == "LEAVE":
                room.mark_player_left_game(pid)
                player.name = ""
                player.vote_category = None
                player.disconnected_at = None
                player.reset_stats()
                room.refresh_category_locks()
                room.apply_votes()
                room.check_empty_active_game()
                await room.broadcast()

            elif kind == "VOTE":
                if not player.joined:
                    continue
                if "beginner" in msg:
                    player.beginner = bool(msg.get("beginner"))
                if "category" in msg and room.phase in ("LOBBY", "PODIUM"):
                    vote_cat = msg.get("category")
                    if vote_cat in room.available_categories():
                        player.vote_category = vote_cat
                    elif vote_cat is None or vote_cat == "":
                        player.vote_category = None
                room.refresh_category_locks()
                room.apply_votes()
                await room.broadcast()

            elif kind == "ANSWER":
                try:
                    choice = int(msg.get("choice"))
                except (TypeError, ValueError):
                    continue
                if (
                    room.phase == "QUESTION"
                    and player.joined
                    and player.answer is None
                    and 0 <= choice <= 3
                ):
                    player.answer = choice  # réponse verrouillée
                    player.answer_remaining = room.remaining_precise()
                    player.missed_streak = 0
                    if room.all_answered():
                        room.reveal()
                    await room.broadcast()

            elif kind == "SHARE":
                if player.joined and room.phase == "PODIUM":
                    room.game_shares_count += 1
                    if pid in room.game_player_stats:
                        room.game_player_stats[pid]["shares"] += 1
                    room.trigger_stats_push()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player.ws is websocket:
            player.ws = None
            if player.joined:
                room.mark_player_left_game(pid)
            if not player.joined:
                room.players.pop(pid, None)
            else:
                player.disconnected_at = time.monotonic()
            room.refresh_category_locks()
            room.apply_votes()
            room.check_empty_active_game()
            await room.broadcast()
