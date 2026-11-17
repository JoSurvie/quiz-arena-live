"""Quiz Arena Live - serveur FastAPI + WebSockets (multi-salles)."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
QUESTIONS_FILE = BASE_DIR / "questions.json"

# Durées des phases (en secondes "de jeu")
LOBBY_DURATION = 240
QUESTION_DURATION = 20
REVEAL_DURATION = 10
PODIUM_DURATION = 60
QUESTIONS_PER_GAME = 10

MIN_POINTS = 500
MAX_POINTS = 1000

CODE_LENGTH = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans caractères ambigus (O/0, I/1)
ROOM_TTL = 30 * 60  # suppression d'une salle vide après 30 minutes
MAX_NAME_LENGTH = 16


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
            valid.append(
                {
                    "category": str(item.get("category", "Général")),
                    "q": item["q"],
                    "options": [str(o) for o in options],
                    "answer": answer,
                }
            )
    if not valid:
        raise RuntimeError("questions.json ne contient aucune question valide.")
    return valid


QUESTIONS = load_questions()


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

    @property
    def joined(self) -> bool:
        return bool(self.name)

    def reset_round(self) -> None:
        self.answer = None
        self.answer_remaining = 0.0


class Room:
    def __init__(self, code: str):
        self.code = code
        self.phase = "LOBBY"
        self.duration = LOBBY_DURATION
        self.timer = LOBBY_DURATION
        self.speed = 1
        self.players: dict[str, Player] = {}
        self.tvs: set[WebSocket] = set()
        self.questions: list[dict[str, Any]] = []
        self.q_index = -1
        self.counts = [0, 0, 0, 0]
        self.task: asyncio.Task | None = None
        self.last_tick = time.monotonic()
        self.empty_since: float | None = None

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

    # ----- transitions ----------------------------------------------------- #
    def set_phase(self, phase: str, duration: int) -> None:
        self.phase = phase
        self.duration = duration
        self.timer = duration
        self.last_tick = time.monotonic()

    def start_game(self) -> None:
        self.questions = random.sample(QUESTIONS, min(QUESTIONS_PER_GAME, len(QUESTIONS)))
        self.q_index = 0
        for p in self.players.values():
            p.score = 0
            p.last_points = 0
            p.last_correct = None
            p.reset_round()
        self.set_phase("QUESTION", QUESTION_DURATION)

    def reveal(self) -> None:
        q = self.current_question()
        self.counts = [0, 0, 0, 0]
        for p in self.joined_players():
            if p.answer is not None:
                self.counts[p.answer] += 1
            if q is not None and p.answer == q["answer"]:
                ratio = p.answer_remaining / QUESTION_DURATION
                pts = round(MIN_POINTS + (MAX_POINTS - MIN_POINTS) * ratio)
                pts = max(MIN_POINTS, min(MAX_POINTS, pts))
                p.score += pts
                p.last_points = pts
                p.last_correct = True
            else:
                p.last_points = 0
                p.last_correct = False
        self.set_phase("REVEAL", REVEAL_DURATION)

    def back_to_lobby(self) -> None:
        self.questions = []
        self.q_index = -1
        for p in self.players.values():
            p.reset_round()
            p.last_points = 0
            p.last_correct = None
        self.set_phase("LOBBY", LOBBY_DURATION)

    def advance(self) -> None:
        """Passe à l'étape suivante (fin de timer ou commande régie NEXT)."""
        if self.phase == "LOBBY":
            self.start_game()
        elif self.phase == "QUESTION":
            self.reveal()
        elif self.phase == "REVEAL":
            if self.q_index + 1 < len(self.questions):
                self.q_index += 1
                for p in self.players.values():
                    p.reset_round()
                self.set_phase("QUESTION", QUESTION_DURATION)
            else:
                self.set_phase("PODIUM", PODIUM_DURATION)
        else:  # PODIUM -> nouveau cycle
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
        return {
            "type": "state",
            "room": self.code,
            "phase": self.phase,
            "timer": max(0, int(self.timer)),
            "duration": self.duration,
            "speed": self.speed,
            "q_index": self.q_index,
            "q_total": len(self.questions),
            "question": self._question_payload(with_answer=reveal),
            "counts": self.counts if reveal else None,
            "answered_count": sum(1 for p in joined if p.answer is not None),
            "players": [self._public_player(p) for p in joined],
            "leaderboard": [self._public_player(p) for p in self.leaderboard()],
        }

    def player_state(self, p: Player) -> dict[str, Any]:
        board = self.leaderboard()
        rank = next((i + 1 for i, x in enumerate(board) if x.id == p.id), None)
        reveal = self.phase == "REVEAL"
        return {
            "type": "state",
            "room": self.code,
            "phase": self.phase,
            "timer": max(0, int(self.timer)),
            "duration": self.duration,
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
            "last_points": p.last_points,
            "last_correct": p.last_correct,
            "top3": [self._public_player(x) for x in board[:3]],
        }

    async def broadcast(self) -> None:
        tasks = []
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
            room.timer -= 1
            room.last_tick = time.monotonic()
            if room.timer <= 0:
                room.on_timer_expired()
            await room.broadcast()
    finally:
        room.empty_since = time.monotonic()


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
app = FastAPI(title="Quiz Arena Live")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "rooms": len(rooms), "questions": len(QUESTIONS)}


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
    room.ensure_loop()
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
                    if room.all_answered():
                        room.reveal()
                    await room.broadcast()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player.ws is websocket:
            player.ws = None
            if not player.joined:
                room.players.pop(pid, None)
            await room.broadcast()
