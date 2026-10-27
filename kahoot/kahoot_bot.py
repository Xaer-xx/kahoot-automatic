#!/usr/bin/env python3
"""
Kahoot auto-player (Playwright, asyncio).

HOW IT WORKS
------------
1. ANSWER KEY. Kahoot's server never tells a player which answer is correct
   until *after* that player has answered, so the live game cannot be "read"
   for the solution. The correct answers have to come from the quiz itself.
   Public quizzes are readable through Kahoot's REST API
   (https://create.kahoot.it/rest/kahoots/<quiz-uuid>), which lists every
   question with a `correct: true/false` flag on each choice. You give the
   script the quiz link / UUID / a title to search, and it builds a key.

2. JOINING. Playwright opens https://kahoot.it, dismisses the cookie banner,
   types the PIN, then the nickname.

3. PACKET INSPECTION (which question are we on?). The game runs over a
   CometD WebSocket (wss://kahoot.it/cometd/<pin>/<token>). Playwright exposes
   every frame via `ws.on("framereceived")`. A frame is a JSON array of CometD
   messages. Game traffic to the player is on channel "/service/player" and
   looks like:

       [{"channel": "/service/player",
         "data": {"id": 2, "content": "{\"questionIndex\": 3, ...}"}}]

   `content` is itself a JSON *string*. We read `questionIndex` from it so we
   know which entry of the answer key applies, and `isCorrect` from the result
   message to report how we did.

4. DOM MONITORING (when can we answer?). An init script injects a
   MutationObserver into the page. The moment the coloured answer buttons
   (`data-functional-selector="answer-0".."answer-3"`) become visible it calls
   back into Python through an exposed binding. That is event-driven, so there
   is no polling delay.

5. ANSWERING. The correct button is clicked directly in the page with
   `element.click()` (one round trip, no actionability waits). If the UI does
   not react within 1.5 s, it retries with a real Playwright mouse click.

Supported question types: single choice ("quiz", incl. true/false) and
multiple-select. Other types (jumble, slider, type-answer, ...) are skipped.

Selectors marked UNVERIFIED below could not be checked without a live game;
run with --debug to dump the real frames and DOM events to kahoot_debug.jsonl
and adjust the constants if Kahoot has changed its markup.
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import (
    APIRequestContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
    WebSocket,
    async_playwright,
)

log = logging.getLogger("kahoot")

JOIN_URL = "https://kahoot.it"
REST_BASE = "https://create.kahoot.it/rest/kahoots"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# --- DOM selectors, tried in order ------------------------------------------
SEL_COOKIE = ["#onetrust-reject-all-handler", "#onetrust-accept-btn-handler"]  # verified
SEL_PIN_INPUT = ['[data-functional-selector="game-pin-input"]', 'input[name="gameId"]']  # verified
SEL_PIN_SUBMIT = ['[data-functional-selector="join-game-pin"]']  # verified
SEL_NICK_INPUT = [  # UNVERIFIED (nickname screen only exists inside a live game)
    '[data-functional-selector="username-input"]',
    'input[name="nickname"]',
    "#nickname",
    'input[type="text"]:not([name="gameId"])',
]
SEL_NICK_SUBMIT = ['[data-functional-selector="join-button-username"]', 'button[type="submit"]']  # UNVERIFIED
SEL_TOAST = '[data-functional-selector="toast-base-content"]'  # verified (error messages appear here)
SEL_ANSWER_PREFIX = '[data-functional-selector^="answer-"]'  # UNVERIFIED
SEL_QUESTION_TEXT = '[data-functional-selector="block-title"]'  # UNVERIFIED, optional
SEL_MULTI_SUBMIT = ['[data-functional-selector="multi-select-submit-button"]']  # UNVERIFIED

SUPPORTED_TYPES = {"quiz", "multiple_select_quiz"}


def answer_selector(index: int) -> str:
    return f'[data-functional-selector="answer-{index}"]'


# --- In-page MutationObserver ------------------------------------------------
# Runs in every new document. Emits "answers_shown" (with a snapshot of the
# buttons) when the answer buttons appear and "answers_gone" when they vanish.
DOM_WATCHER_JS = """
(() => {
  if (window.top !== window) return;
  const ANSWER_SEL = __ANSWER_SEL__;
  const TEXT_SEL = __TEXT_SEL__;
  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const buttons = () => [...document.querySelectorAll(ANSWER_SEL)].filter(
      (e) => /^answer-\\d+$/.test(e.getAttribute('data-functional-selector')) && visible(e));
  const emit = (p) => { try { window.__kahootEvent(JSON.stringify(p)); } catch (_) {} };
  let showing = false;
  const check = () => {
    const btns = buttons();
    const now = btns.length > 0;
    if (now === showing) return;
    showing = now;
    if (!now) { emit({kind: 'answers_gone'}); return; }
    const q = document.querySelector(TEXT_SEL);
    emit({
      kind: 'answers_shown',
      question: q ? q.innerText.trim() : '',
      pageText: (document.body ? document.body.innerText : '').slice(0, 3000),
      buttons: btns.map((b) => ({
        index: parseInt(b.getAttribute('data-functional-selector').slice(7), 10),
        text: (b.innerText || '').trim(),
      })),
    });
  };
  const start = () => {
    new MutationObserver(check).observe(document.documentElement, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ['style', 'class', 'hidden', 'data-functional-selector'],
    });
    setInterval(check, 250);  // safety net if a mutation is ever missed
    check();
  };
  if (document.documentElement) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
""".replace("__ANSWER_SEL__", json.dumps(SEL_ANSWER_PREFIX)).replace("__TEXT_SEL__", json.dumps(SEL_QUESTION_TEXT))

# Kahoot ignores synthetic element.click() (it needs trusted pointer events), so answers are
# clicked with the real mouse at the button's centre.
JS_CENTER = ("(sel) => { const e = document.querySelector(sel); if (!e) return null;"
             " const r = e.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; }")


# =============================================================================
# Answer key (pure functions, unit-tested in test_kahoot_bot.py)
# =============================================================================
def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def norm(text: str) -> str:
    return re.sub(r"\W+", " ", strip_html(text).casefold()).strip()


@dataclass
class KeyQuestion:
    index: int
    type: str
    text: str
    choices: list[str]
    correct: list[int]  # indices into `choices`


@dataclass
class AnswerKey:
    title: str
    questions: list[KeyQuestion]


def parse_quiz(data: dict[str, Any]) -> AnswerKey:
    """Turn the REST payload of a quiz into an AnswerKey."""
    questions = []
    for i, q in enumerate(data.get("questions") or []):
        choices = q.get("choices") or []
        questions.append(
            KeyQuestion(
                index=i,
                type=q.get("type", ""),
                text=strip_html(q.get("question") or q.get("title") or ""),
                choices=[strip_html(c.get("answer") or "") for c in choices],
                correct=[j for j, c in enumerate(choices) if c.get("correct")],
            )
        )
    return AnswerKey(title=data.get("title", "?"), questions=questions)


def find_quiz_payload(obj: Any) -> Optional[dict]:
    """Depth-first search for a dict that looks like a full quiz (questions whose choices carry
    a `correct` flag). Used on whatever JSON the join flow / challenge endpoint hands us."""
    if isinstance(obj, dict):
        qs = obj.get("questions")
        if isinstance(qs, list) and qs and isinstance(qs[0], dict):
            choices = qs[0].get("choices")
            if isinstance(choices, list) and any(isinstance(c, dict) and "correct" in c for c in choices):
                return obj
        children: Any = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        hit = find_quiz_payload(child)
        if hit:
            return hit
    return None


# JSON keys whose string values are worth searching for (quiz title, question text, ...).
TEXT_KEYS = {"quiztitle", "kahoottitle", "quizname", "title", "name", "question", "questiontitle", "blocktitle", "text"}


def quiz_hints(text: str) -> tuple[set[str], set[str]]:
    """(candidate quiz UUIDs, candidate search texts such as a quiz title or question) in a JSON text."""
    uuids = {m.lower() for m in UUID_RE.findall(text)}
    texts: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in TEXT_KEYS and isinstance(v, str):
                    s = strip_html(v)
                    if len(s) >= 4 and not UUID_RE.fullmatch(s):
                        texts.add(s)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    try:
        walk(json.loads(text))
    except (TypeError, ValueError):
        pass
    return uuids, texts


def fingerprint_score(key: AnswerKey, fingerprint: list[int]) -> float:
    """Share of questions whose number of choices matches `quizQuestionAnswers`, the per-question
    answer counts Kahoot sends every player at quiz start (e.g. [4, 4, 2, 4]). Content slides may or
    may not be counted, so both variants are tried."""
    best = 0.0
    for counts in ([len(q.choices) for q in key.questions],
                   [len(q.choices) for q in key.questions if q.type != "content"]):
        if fingerprint and len(counts) == len(fingerprint):
            best = max(best, sum(a == b for a, b in zip(counts, fingerprint)) / len(fingerprint))
    return best


def best_text_ratio(key: AnswerKey, text: str) -> float:
    """How well `text` matches the closest question of the quiz (0..1)."""
    target = norm(text)
    if not target:
        return 0.0
    return max((difflib.SequenceMatcher(None, target, norm(q.text)).ratio() for q in key.questions), default=0.0)


def page_text_candidates(page_text: str, button_texts: list[str], nickname: str) -> list[str]:
    """Sentence-like lines on the player screen (likely the question when the host shows it on
    devices). Only used as search queries, never trusted on their own."""
    skip = {norm(t) for t in button_texts} | {norm(nickname)}
    lines = {line.strip() for line in page_text.splitlines()}
    good = [s for s in lines if len(s) >= 15 and len(s.split()) >= 3 and norm(s) not in skip]
    return sorted(good, key=len, reverse=True)[:2]


def match_by_text(key: AnswerKey, dom_text: str) -> Optional[KeyQuestion]:
    """Fuzzy-match question text read from the page; None if unsure."""
    target = norm(dom_text)
    if not target:
        return None
    scored = sorted(
        ((difflib.SequenceMatcher(None, target, norm(q.text)).ratio(), q.index) for q in key.questions),
        reverse=True,
    )
    if not scored or scored[0][0] < 0.9:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None  # ambiguous (duplicate question text) -> let the index decide
    return key.questions[scored[0][1]]


def resolve_question(
    key: Optional[AnswerKey], ws_index: Optional[int], seq_index: int, dom_text: str
) -> Optional[KeyQuestion]:
    """Which key entry is the question on screen? Text match > WS index > counter."""
    if key is None or not key.questions:
        return None
    if dom_text:
        hit = match_by_text(key, dom_text)
        if hit:
            return hit
    idx = ws_index if ws_index is not None else seq_index
    return key.questions[idx] if 0 <= idx < len(key.questions) else None


def pick_buttons(q: KeyQuestion, buttons: list[dict[str, Any]]) -> list[int]:
    """Button indices to press. Uses answer text when the page shows it (robust
    to Kahoot's answer shuffling); otherwise the choice's position."""
    available = [b["index"] for b in buttons]
    texts = {b["index"]: norm(b.get("text", "")) for b in buttons}
    if all(texts.values()) and all(norm(c) for c in q.choices):
        by_text = {t: i for i, t in texts.items()}
        targets = [by_text.get(norm(q.choices[c])) for c in q.correct]
        if targets and None not in targets:
            return targets  # type: ignore[return-value]
    return [c for c in q.correct if c in available]


@dataclass
class Plan:
    indexes: list[int]
    multi: bool
    source: str


def plan_answer(
    q: Optional[KeyQuestion],
    buttons: list[dict[str, Any]],
    fallback: str,
    rng: random.Random = random.Random(),
) -> Plan:
    if q is not None and q.type in SUPPORTED_TYPES:
        targets = pick_buttons(q, buttons)
        if targets:
            return Plan(targets, q.type == "multiple_select_quiz" or len(targets) > 1, "answer key")
    why = "no answer key" if q is None else f"unsupported/unresolvable question type '{q.type}'"
    if fallback == "random" and buttons:
        return Plan([rng.choice(buttons)["index"]], False, f"random guess ({why})")
    return Plan([], False, f"skipped ({why})")


# =============================================================================
# CometD frame parsing (pure)
# =============================================================================
def parse_cometd(payload: str | bytes) -> list[dict[str, Any]]:
    """Return the `content` dicts of every /service/player message in a frame.

    A frame is a JSON array of CometD messages; `data.content` is a JSON string.
    Each returned dict also carries the message type as `_id` (data.id)."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        frame = json.loads(payload)
    except (TypeError, ValueError):
        return []
    out = []
    for msg in frame if isinstance(frame, list) else [frame]:
        if not isinstance(msg, dict) or msg.get("channel") != "/service/player":
            continue
        data = msg.get("data")
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {}
        out.append({**(content if isinstance(content, dict) else {}), "_id": data.get("id")})
    return out


# =============================================================================
# Fetching the answer key
# =============================================================================
async def fetch_json(request: APIRequestContext, url: str, params: Optional[dict] = None) -> Any:
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = await request.get(url, params=params, timeout=15_000)
            if resp.status in (401, 403, 404):
                raise LookupError(f"HTTP {resp.status} (private or deleted quiz?)")
            if not resp.ok:
                raise PlaywrightError(f"HTTP {resp.status}")
            return await resp.json()
        except LookupError:
            raise
        except Exception as exc:  # network hiccup -> retry
            last = exc
            await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last}")


async def search_quizzes(request: APIRequestContext, query: str) -> list[dict]:
    data = await fetch_json(
        request,
        f"{REST_BASE}/",
        params={"query": query, "limit": 8, "orderBy": "relevance", "cursor": 0,
                "searchCluster": 1, "includeExtendedCounters": "false"},
    )
    return [e["card"] for e in data.get("entities", []) if "card" in e]


async def load_answer_key(request: APIRequestContext, spec: str) -> Optional[AnswerKey]:
    """`spec` may be a quiz UUID, a Kahoot URL containing one, or a title to search."""
    spec = spec.strip()
    if not spec:
        return None
    m = UUID_RE.search(spec)
    uuid = m.group(0).lower() if m else None
    if uuid is None:
        cards = await search_quizzes(request, spec)
        if not cards:
            log.warning("No public quiz found for %r", spec)
            return None
        print("\nMatching public quizzes:")
        for n, c in enumerate(cards, 1):
            print(f"  {n}. {c.get('title')}  [{c.get('number_of_questions')} questions, by {c.get('creator_username')}]")
        choice = await asyncio.to_thread(input, "Pick a number (blank = none): ")
        if not choice.strip().isdigit() or not 1 <= int(choice) <= len(cards):
            return None
        uuid = cards[int(choice) - 1]["uuid"]
    key = parse_quiz(await fetch_json(request, f"{REST_BASE}/{uuid}"))
    log.info("Answer key loaded: %r (%d questions)", key.title, len(key.questions))
    return key


# =============================================================================
# The bot
# =============================================================================
class JoinError(RuntimeError):
    pass


async def solve_with_claude(
    request: APIRequestContext, api_key: str, model: str, question: str, buttons: list[dict[str, Any]]
) -> list[int]:
    """Ask Claude which button(s) are correct. Needs the question and every answer as text on
    the player screen; returns [] if that text is missing or the call fails."""
    if not question or not all(b.get("text") for b in buttons):
        return []
    options = "\n".join(f"{b['index'] + 1}. {b['text']}" for b in buttons)
    prompt = (f"Quiz question: {question}\n\nOptions:\n{options}\n\n"
              "Reply with ONLY the number of the correct option (several numbers separated by commas "
              "only if the question asks to select all that apply).")
    try:
        resp = await request.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            data=json.dumps({"model": model, "max_tokens": 20, "messages": [{"role": "user", "content": prompt}]}),
            timeout=8000,
        )
        if not resp.ok:
            log.warning("Claude API returned HTTP %s", resp.status)
            return []
        text = (await resp.json())["content"][0]["text"]
    except Exception as exc:
        log.warning("Claude call failed: %s", exc)
        return []
    valid = {b["index"] for b in buttons}
    picks = [int(n) - 1 for n in re.findall(r"\d+", text) if int(n) - 1 in valid]
    return list(dict.fromkeys(picks))


class KahootBot:
    def __init__(self, key: Optional[AnswerKey], fallback: str, delay: float, debug_path: Optional[Path],
                 solver=None, nickname: str = "", max_wait: float = 3.0):
        self.key, self.fallback, self.delay = key, fallback, delay
        self.solver = solver  # async (question, buttons) -> list[int]; used when the key has no answer
        self.nickname = nickname
        self.max_wait = max_wait  # how long a question may wait for the quiz to be identified
        self.debug_path = debug_path
        # --- automatic quiz discovery (see _ingest / _discover) ---
        self.request: Optional[APIRequestContext] = None
        self.hint_uuids: set[str] = set()
        self.queries: list[str] = []  # texts to search for (titles, question texts)
        self.seen_questions: list[str] = []  # question texts read from the page (trusted)
        self.tried: set[str] = set()
        self.candidates: dict[str, tuple[AnswerKey, str]] = {}  # fetched, not yet confirmed
        self._quiz_cache: dict[str, Optional[AnswerKey]] = {}
        self.fingerprint: Optional[list[int]] = None  # quizQuestionAnswers from the game
        self.game_total: Optional[int] = None  # number of questions the live game reports
        self.discover_event = asyncio.Event()  # "new hints, run a discovery pass"
        self.pass_done = asyncio.Event()
        # --- game state ---
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.gone = asyncio.Event()  # set when the answer buttons disappear
        self.ws_changed = asyncio.Event()  # set when the WS reports a new question index
        self.ws_index: Optional[int] = None
        self.answered_index: Optional[int] = None
        self.seq = 0  # number of answer screens seen (fallback question counter)
        self.wrong_streak = 0
        self._warned_count = False
        self._seen_ids: set[Any] = set()

    # ---- debug ---------------------------------------------------------------
    def _dump(self, kind: str, payload: Any) -> None:
        if self.debug_path:
            with self.debug_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "kind": kind, "payload": payload}, default=str) + "\n")

    # ---- wiring --------------------------------------------------------------
    async def attach(self, page: Page) -> None:
        """Install all hooks. Must run before the first navigation."""
        await page.expose_function("__kahootEvent", self._on_dom_event)
        await page.add_init_script(DOM_WATCHER_JS)
        page.on("websocket", self._on_websocket)
        page.on("response", lambda r: asyncio.ensure_future(self._on_response(r)))

    # ---- automatic quiz discovery -----------------------------------------------
    # Like the online "answer solvers": everything Kahoot tells the player (join response, socket
    # messages, question text on screen) is scanned for a quiz id or searchable text. Search hits
    # are only accepted when the live game confirms them - above all the per-question answer counts
    # (`quizQuestionAnswers`), which identify a quiz almost uniquely.
    async def _on_response(self, resp) -> None:
        if not any(p in resp.url for p in ("/reserve/session/", "/rest/challenges/")):
            return
        try:
            text = await resp.text()
        except PlaywrightError:
            return
        self._dump("http_response", {"url": resp.url, "status": resp.status, "body": text[:4000]})
        self._ingest(text)

    def _add_query(self, text: str) -> bool:
        text = text.strip()
        if not text or text in self.tried or text in self.queries or norm(text) == norm(self.nickname):
            return False
        self.queries.append(text)
        return True

    def _ingest(self, text: str) -> None:
        if self.key is not None:
            return
        try:
            quiz = find_quiz_payload(json.loads(text))
        except ValueError:
            quiz = None
        if quiz:
            self._set_key(parse_quiz(quiz), "full quiz found in game data")
            return
        uuids, texts = quiz_hints(text)
        new = bool(uuids - self.tried - self.hint_uuids)
        self.hint_uuids |= uuids
        for t in texts:
            new |= self._add_query(t)
        if new:
            self.discover_event.set()

    def _set_key(self, key: AnswerKey, evidence: str) -> None:
        self.key = key
        log.info("QUIZ IDENTIFIED: %r (%d questions) via %s - answers ready", key.title, len(key.questions), evidence)

    def _verify(self, key: AnswerKey, query: str = "") -> tuple[bool, str]:
        """(ok, evidence). ok=False: the live game contradicts this quiz. evidence='': nothing
        contradicts it, but nothing confirms it either."""
        if not any(q.correct for q in key.questions):
            return False, ""
        evidence = ""
        if self.fingerprint:
            if fingerprint_score(key, self.fingerprint) < 0.9:
                return False, ""
            evidence = "answer-count fingerprint"
        elif self.game_total and self.game_total not in (
                len(key.questions), len([q for q in key.questions if q.type != "content"])):
            return False, ""
        for t in self.seen_questions:
            if best_text_ratio(key, t) < 0.85:
                return False, ""
            evidence = evidence or "question text on screen"
        if not evidence and query and self.game_total:  # weaker: needs the question count to agree
            if best_text_ratio(key, query) >= 0.9:
                evidence = "contains this question + same question count"
            elif norm(key.title) == norm(query):
                evidence = "exact title + same question count"
        return True, evidence

    async def _fetch_quiz(self, uuid: str) -> Optional[AnswerKey]:
        if uuid not in self._quiz_cache:
            try:
                self._quiz_cache[uuid] = parse_quiz(await fetch_json(self.request, f"{REST_BASE}/{uuid}"))
            except (LookupError, RuntimeError):
                self._quiz_cache[uuid] = None  # not a public quiz (e.g. the id of the game session)
        return self._quiz_cache[uuid]

    async def discover_worker(self) -> None:
        while True:
            await self.discover_event.wait()
            self.discover_event.clear()
            if self.key is None and self.request is not None:
                try:
                    await self._discover()
                except Exception:
                    log.exception("Quiz discovery failed")
            self.pass_done.set()

    async def _discover(self) -> None:
        # 1) quizzes fetched earlier: new game info (fingerprint, question text) may now confirm one
        for uuid, (key, query) in list(self.candidates.items()):
            ok, evidence = self._verify(key, query)
            if not ok:
                del self.candidates[uuid]
            elif evidence:
                return self._set_key(key, evidence)
        # 2) quiz ids seen in the game data
        for uuid in sorted(self.hint_uuids - self.tried):
            self.tried.add(uuid)
            key = await self._fetch_quiz(uuid)
            if key is not None:
                ok, evidence = self._verify(key)
                if ok:
                    return self._set_key(key, evidence or "quiz id in game data")
        # 3) search Kahoot for every text we have (it indexes titles AND question texts)
        for query in [q for q in self.queries if q not in self.tried]:
            self.tried.add(query)
            try:
                cards = await search_quizzes(self.request, query[:200])
            except (LookupError, RuntimeError):
                continue
            sizes = {n for n in (self.game_total, len(self.fingerprint or [])) if n}
            sized = [c for c in cards if c.get("number_of_questions") in sizes]
            for card in (sized or cards)[:5]:
                key = await self._fetch_quiz(card["uuid"])
                if key is None:
                    continue
                ok, evidence = self._verify(key, query)
                if ok and evidence:
                    return self._set_key(key, evidence)
                if ok:
                    self.candidates[card["uuid"]] = (key, query)
            log.info("Searched Kahoot for %r: no confirmed match yet", query[:60])

    async def _await_discovery(self) -> None:
        """Wait (up to max_wait) for the discovery passes triggered by this question."""
        deadline = time.monotonic() + self.max_wait
        while self.key is None:
            self.pass_done.clear()
            try:
                await asyncio.wait_for(self.pass_done.wait(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return
            if not self.discover_event.is_set():  # no further pass pending
                return

    # ---- WebSocket inspection -------------------------------------------------
    def _on_websocket(self, ws: WebSocket) -> None:
        if "cometd" not in ws.url:
            return
        log.info("Game socket open: %s", re.sub(r"(/cometd/\d+/).*", r"\1...", ws.url))
        ws.on("framereceived", self._on_frame)
        ws.on("close", lambda *_: log.warning("Game socket closed"))

    def _on_frame(self, payload: str | bytes) -> None:
        text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
        self._dump("ws_frame", text)
        for content in parse_cometd(payload):
            self._on_player_message(content)
            self._ingest(json.dumps(content))  # content is already unwrapped from its JSON string

    def _on_player_message(self, c: dict[str, Any]) -> None:
        msg_id = c.get("_id")
        if msg_id not in self._seen_ids:  # shows which fields Kahoot sends (helps adapting the bot)
            self._seen_ids.add(msg_id)
            log.info("Game data (message id %s): %s", msg_id, ", ".join(sorted(k for k in c if k != "_id")) or "-")
        qi = c.get("questionIndex")
        if isinstance(qi, int) and qi != self.ws_index:
            self.ws_index = qi
            self.ws_changed.set()
            log.debug("Server says question index = %d (block type %s)", qi, c.get("gameBlockType"))
        total = c.get("totalGameBlockCount")
        if isinstance(total, int):
            self.game_total = total
        qqa = c.get("quizQuestionAnswers")
        if isinstance(qqa, list) and qqa and all(isinstance(n, int) for n in qqa) and qqa != self.fingerprint:
            self.fingerprint = qqa
            log.info("Game fingerprint: %d questions, answers per question %s", len(qqa), qqa)
            self.discover_event.set()  # may confirm a quiz fetched earlier
        if self.key and isinstance(total, int) and total != len(self.key.questions) and not self._warned_count:
            self._warned_count = True
            log.warning("Live game has %d questions but the answer key has %d - wrong quiz?", total, len(self.key.questions))
        if isinstance(c.get("isCorrect"), bool):  # result message after we answered
            self.wrong_streak = 0 if c["isCorrect"] else self.wrong_streak + 1
            log.info("Result: %s (points: %s)", "CORRECT" if c["isCorrect"] else "wrong", c.get("totalScore", c.get("points", "?")))
            if self.wrong_streak >= 2 and self.key:
                log.warning("%d wrong in a row - the answer key probably does not match this game", self.wrong_streak)

    # ---- DOM events -----------------------------------------------------------
    def _on_dom_event(self, raw: str) -> None:
        evt = json.loads(raw)
        evt["_rx"] = time.perf_counter()
        self._dump("dom_event", evt)
        if evt["kind"] == "answers_gone":
            self.gone.set()
        elif evt["kind"] == "answers_shown":
            self.gone.clear()
            self.queue.put_nowait(evt)

    # ---- answering ------------------------------------------------------------
    async def answer_worker(self, page: Page) -> None:
        while True:
            evt = await self.queue.get()
            try:
                await self._handle_question(page, evt)
            except Exception:  # never let one bad question kill the loop
                log.exception("Failed to answer this question")

    async def _handle_question(self, page: Page, evt: dict[str, Any]) -> None:
        self.seq += 1
        buttons = evt["buttons"]
        question = (evt.get("question") or "").strip()
        if self.key is None and self.request is not None:
            # Use what this question reveals to identify the quiz, then answer from the key.
            new = False
            if question and question not in self.seen_questions:
                self.seen_questions.append(question)
                new = True
            extra = page_text_candidates(evt.get("pageText", ""), [b["text"] for b in buttons], self.nickname)
            for text in ([question] if question else []) + extra:
                new |= self._add_query(text)
            if new:
                self.discover_event.set()
                await self._await_discovery()
        # The WS frame carrying the new index normally lands seconds earlier, but if the
        # DOM won the race, give it a moment so we don't use the previous question's index.
        if self.ws_index is not None and self.ws_index == self.answered_index:
            self.ws_changed.clear()
            try:
                await asyncio.wait_for(self.ws_changed.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
        q = resolve_question(self.key, self.ws_index, self.seq - 1, question)
        plan = plan_answer(q, buttons, self.fallback)
        if self.solver and not plan.source.startswith("answer key"):
            picks = await self.solver(question, buttons)
            if picks:
                plan = Plan(picks, len(picks) > 1, "Claude")
        label = f"Q{q.index + 1} {q.text[:60]!r}" if q else f"question #{self.seq}"
        log.info("%s -> buttons %s via %s", label, plan.indexes or "none", plan.source)
        if not plan.indexes:
            return
        if self.delay:
            await asyncio.sleep(self.delay)
        clicked = await self._click(page, plan)
        log.info("Answered %.0f ms after the buttons appeared", (clicked - evt["_rx"]) * 1000)
        self.answered_index = self.ws_index

    async def _press(self, page: Page, index: int) -> None:
        sel = answer_selector(index)
        point = await page.evaluate(JS_CENTER, sel)
        if point:
            await page.mouse.click(point[0], point[1])
        else:
            await page.locator(sel).click(timeout=2000, force=True)

    async def _click(self, page: Page, plan: Plan) -> float:
        """Press the planned buttons; returns the perf_counter time of the (last) click."""
        for i in plan.indexes:
            await self._press(page, i)
        if plan.multi:
            await self._submit_multi(page)
        clicked = time.perf_counter()
        try:
            await asyncio.wait_for(self.gone.wait(), 1.0)  # answer screen changed => click registered
        except asyncio.TimeoutError:
            log.warning("Answer screen did not change; clicking again")
            if plan.multi:
                await self._submit_multi(page)
            else:
                await page.locator(answer_selector(plan.indexes[0])).click(timeout=2000, force=True)
        return clicked

    async def _submit_multi(self, page: Page) -> None:
        for sel in SEL_MULTI_SUBMIT:
            point = await page.evaluate(JS_CENTER, sel)
            if point:
                await page.mouse.click(point[0], point[1])
                return
        await page.get_by_role("button", name=re.compile(r"submit|send|^ok", re.I)).first.click(timeout=2000, force=True)


# =============================================================================
# Joining
# =============================================================================
async def find_visible(page: Page, selectors: list[str], timeout: float):
    """First selector (in priority order) that is visible within `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    return loc
            except PlaywrightError:
                pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


async def safe_click(loc) -> None:
    """Click, but never stall: animated overlays (cookie banner) can make Playwright's
    strict actionability checks wait out the full timeout, so fall back to an in-page click."""
    try:
        await loc.click(timeout=2000)
    except PlaywrightError:
        try:
            await loc.evaluate("(e) => e.click()", timeout=2000)
        except PlaywrightError as exc:
            log.debug("Click failed twice: %s", exc)


async def page_message(page: Page) -> str:
    """Text of the first non-empty error/toast node ('' if none). Kahoot renders errors such as
    "We didn't recognize that game PIN" in a role="alert" node; empty alert/toast containers also
    exist on the page, so every candidate has to be checked, not just the first."""
    try:
        texts = await page.evaluate(
            "(sels) => sels.flatMap((s) => [...document.querySelectorAll(s)])"
            ".map((e) => (e.innerText || '').trim()).filter(Boolean)",
            [SEL_TOAST, '[role="alert"]', '[data-functional-selector*="error"]'],
        )
    except PlaywrightError:
        return ""
    return texts[0] if texts else ""


async def join_game(page: Page, pin: str, nickname: str) -> None:
    log.info("Opening %s", JOIN_URL)
    await page.goto(JOIN_URL, wait_until="domcontentloaded")

    cookie = await find_visible(page, SEL_COOKIE, 4)
    if cookie:
        await safe_click(cookie)

    pin_box = await find_visible(page, SEL_PIN_INPUT, 15)
    if pin_box is None:
        raise JoinError("PIN field not found - Kahoot may have changed the join page (see SEL_PIN_INPUT).")
    await pin_box.fill(pin)
    submit = await find_visible(page, SEL_PIN_SUBMIT, 2)
    await (safe_click(submit) if submit else pin_box.press("Enter"))
    log.info("PIN %s submitted", pin)

    # Wait for the PIN screen to be replaced by the nickname screen. If Kahoot shows an error
    # instead (bad PIN, game locked, ...) it appears within ~1 s, so fail fast on it.
    started = time.monotonic()
    while await pin_box.is_visible():
        elapsed = time.monotonic() - started
        msg = await page_message(page) if elapsed > 1.0 else ""
        if msg:
            raise JoinError(f"Kahoot rejected the PIN: {msg}")
        if elapsed > 20:
            raise JoinError("Timed out waiting for the nickname screen (no error shown).")
        await asyncio.sleep(0.2)

    nick_box = await find_visible(page, SEL_NICK_INPUT, 10)
    if nick_box is None:
        raise JoinError(f"Nickname field not found ({await page_message(page) or 'no message'}); see SEL_NICK_INPUT.")
    await nick_box.fill(nickname)
    await nick_box.press("Enter")
    await asyncio.sleep(1.5)
    if await nick_box.is_visible():  # Enter did nothing -> click the button
        btn = await find_visible(page, SEL_NICK_SUBMIT, 2)
        if btn:
            await safe_click(btn)
    started = time.monotonic()
    while await nick_box.is_visible():
        elapsed = time.monotonic() - started
        msg = await page_message(page) if elapsed > 1.0 else ""
        if msg:
            raise JoinError(f"Kahoot rejected the nickname: {msg}")
        if elapsed > 15:
            raise JoinError("Join stalled after entering the nickname (no error shown).")
        await asyncio.sleep(0.2)
    log.info("Joined as %r - waiting for the host to start. Press Ctrl+C to quit.", nickname)


# =============================================================================
# Entry point
# =============================================================================
async def run(args: argparse.Namespace) -> int:
    async with async_playwright() as pw:
        req = await pw.request.new_context(extra_http_headers={"User-Agent": "Mozilla/5.0"})
        key = None
        if args.quiz:
            try:
                key = await load_answer_key(req, args.quiz)
            except (LookupError, RuntimeError) as exc:
                log.error("Could not load answer key: %s", exc)

        # Claude is the last resort: it is only asked when no key entry answers a question
        # (the key may still be discovered automatically after joining).
        solver = None
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key and args.solver != "none":
            ai_req = await pw.request.new_context()

            async def solver(question, buttons):
                return await solve_with_claude(ai_req, api_key, args.model, question, buttons)

        debug_path = Path("kahoot_debug.jsonl") if args.debug else None
        bot = KahootBot(key, args.fallback, args.delay, debug_path, solver, args.name, args.max_wait)
        bot.request = req

        if key is None:
            # "Challenge" games expose the whole quiz (with answers) by PIN. Live games usually don't.
            try:
                bot._ingest(json.dumps(await fetch_json(req, f"{JOIN_URL}/rest/challenges/pin/{args.pin}")))
            except (LookupError, RuntimeError):
                pass
        if bot.key is None:
            log.info("No key yet - will try to identify the quiz from the game data after joining%s",
                     "; Claude is the fallback" if solver else "")

        browser = await getattr(pw, args.browser).launch(headless=args.headless)
        context = await browser.new_context(viewport={"width": 480, "height": 900}, locale="en-US")
        page = await context.new_page()
        closed = asyncio.Event()
        page.on("close", lambda *_: closed.set())

        await bot.attach(page)
        tasks = [asyncio.create_task(bot.answer_worker(page)), asyncio.create_task(bot.discover_worker())]
        try:
            await join_game(page, args.pin, args.name)
            await closed.wait()  # play until the browser window is closed / Ctrl+C
        except JoinError as exc:
            log.error("%s", exc)
            return 1
        except PlaywrightTimeout as exc:
            log.error("Timed out: %s", exc)
            return 1
        finally:
            for t in tasks:
                t.cancel()
            await browser.close()
            await req.dispose()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-player for Kahoot (needs the quiz's answer key).")
    ap.add_argument("--pin", help="Game PIN (prompted if omitted)")
    ap.add_argument("--name", help="Nickname, max 15 chars (prompted if omitted)")
    ap.add_argument("--quiz", help="Quiz link, UUID, or title to search for the answer key (prompted if omitted)")
    ap.add_argument("--fallback", choices=["random", "skip"], default="random",
                    help="What to do when no key entry applies (default: random)")
    ap.add_argument("--solver", choices=["claude", "none"], default="claude",
                    help="Without an answer key, let Claude answer (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--model", default="claude-haiku-5-5", help="Claude model for --solver claude")
    ap.add_argument("--max-wait", type=float, default=3.0,
                    help="Max seconds a question may wait while the quiz is being identified (default 3)")
    ap.add_argument("--delay", type=float, default=0.0, help="Seconds to wait before clicking (default 0)")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    ap.add_argument("--headless", action="store_true", help="Hide the browser window")
    ap.add_argument("--debug", action="store_true", help="Verbose log + dump frames/DOM events to kahoot_debug.jsonl")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    try:
        args.pin = (args.pin or input("Game PIN: ")).strip()
        args.name = (args.name or input("Nickname: ")).strip()
        if args.quiz is None:
            args.quiz = input("Quiz link / UUID / title (blank = detect the quiz automatically): ")
    except (EOFError, KeyboardInterrupt):
        return 130
    if not args.pin.isdigit():
        print("The PIN must be digits only.", file=sys.stderr)
        return 2
    if not 1 <= len(args.name) <= 15:
        print("The nickname must be 1-15 characters.", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
