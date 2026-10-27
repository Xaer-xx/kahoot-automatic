"""Offline tests: pure logic + an end-to-end run against a mock Kahoot page.

    python -m unittest -v test_kahoot_bot
"""
import asyncio
import base64
import hashlib
import json
import random
import re
import struct
import unittest

from playwright.async_api import async_playwright

import kahoot_bot as kb

QUIZ = {
    "title": "Demo",
    "questions": [
        {"type": "quiz", "question": "Pick C",
         "choices": [{"answer": "A", "correct": False}, {"answer": "B", "correct": False},
                     {"answer": "C", "correct": True}, {"answer": "D", "correct": False}]},
        {"type": "quiz", "question": "Capital of the UK?",
         "choices": [{"answer": "Berlin", "correct": False}, {"answer": "Paris", "correct": False},
                     {"answer": "Madrid", "correct": False}, {"answer": "<b>London</b>", "correct": True}]},
        {"type": "multiple_select_quiz", "question": "Pick the vowels",
         "choices": [{"answer": "a", "correct": True}, {"answer": "b", "correct": False},
                     {"answer": "e", "correct": True}, {"answer": "z", "correct": False}]},
        {"type": "jumble", "question": "Order these",
         "choices": [{"answer": "1"}, {"answer": "2"}]},
    ],
}


def btns(*texts):
    return [{"index": i, "text": t} for i, t in enumerate(texts)]


class PureLogic(unittest.TestCase):
    def setUp(self):
        self.key = kb.parse_quiz(QUIZ)

    def test_parse_quiz(self):
        self.assertEqual(len(self.key.questions), 4)
        self.assertEqual(self.key.questions[0].correct, [2])
        self.assertEqual(self.key.questions[1].choices[3], "London")  # html stripped
        self.assertEqual(self.key.questions[2].correct, [0, 2])

    def test_parse_cometd_nested_json_content(self):
        frame = json.dumps([
            {"channel": "/meta/connect", "successful": True},
            {"channel": "/service/player",
             "data": {"id": 2, "content": json.dumps({"questionIndex": 3, "gameBlockType": "quiz"})}},
        ])
        msgs = kb.parse_cometd(frame)
        self.assertEqual(len(msgs), 1)
        self.assertEqual((msgs[0]["questionIndex"], msgs[0]["_id"]), (3, 2))

    def test_parse_cometd_garbage(self):
        self.assertEqual(kb.parse_cometd("not json"), [])
        self.assertEqual(kb.parse_cometd(b"\xff\xfe"), [])
        self.assertEqual(kb.parse_cometd('{"channel": "/service/player", "data": "x"}'), [])

    def test_pick_by_position_when_no_text(self):
        self.assertEqual(kb.pick_buttons(self.key.questions[0], btns("", "", "", "")), [2])

    def test_pick_by_text_survives_shuffle(self):
        shuffled = btns("Paris", "London", "Berlin", "Madrid")
        self.assertEqual(kb.pick_buttons(self.key.questions[1], shuffled), [1])

    def test_pick_multi(self):
        self.assertEqual(kb.pick_buttons(self.key.questions[2], btns("", "", "", "")), [0, 2])

    def test_resolve_priority(self):
        k = self.key
        self.assertEqual(kb.resolve_question(k, 1, 0, "").index, 1)           # ws index wins over counter
        self.assertEqual(kb.resolve_question(k, None, 2, "").index, 2)        # counter as fallback
        self.assertEqual(kb.resolve_question(k, 0, 0, "capital of the UK").index, 1)  # text beats index
        self.assertIsNone(kb.resolve_question(k, 99, 0, ""))                  # out of range
        self.assertIsNone(kb.resolve_question(None, 0, 0, ""))

    def test_plan_fallbacks(self):
        rng = random.Random(1)
        unsupported = self.key.questions[3]
        self.assertEqual(kb.plan_answer(None, btns("", "", "", ""), "skip").indexes, [])
        p = kb.plan_answer(unsupported, btns("", "", "", ""), "random", rng)
        self.assertEqual(len(p.indexes), 1)
        self.assertIn("random", p.source)
        p = kb.plan_answer(self.key.questions[2], btns("", "", "", ""), "skip")
        self.assertTrue(p.multi)


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status, self.ok = body, status, 200 <= status < 300

    async def json(self):
        return self._body


class FakeRequest:
    """Stands in for Playwright's APIRequestContext: routes by URL to canned JSON."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                return FakeResponse(body)
        return FakeResponse({}, 404)


UUID_A = "7c4ef3fe-7266-4a6b-861b-15719738ad2c"
UUID_GAME = "11111111-2222-4333-8444-555555555555"


class Discovery(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, routes):
        bot = kb.KahootBot(None, "skip", 0, None)
        bot.request = FakeRequest(routes)
        return bot

    def test_find_quiz_payload_and_hints(self):
        wrapped = {"challenge": {"kahoot": QUIZ}}
        self.assertIs(kb.find_quiz_payload(wrapped), QUIZ)
        self.assertIsNone(kb.find_quiz_payload({"questions": [{"choices": [{"answer": "x"}]}]}))  # no answers
        uuids, titles = kb.quiz_hints(json.dumps({"liveGameId": UUID_GAME, "quizTitle": " My Quiz "}))
        self.assertEqual(uuids, {UUID_GAME})
        self.assertEqual(titles, {"My Quiz"})

    def test_ingest_full_quiz_sets_key_directly(self):
        bot = self.make_bot({})
        bot._ingest(json.dumps({"challenge": {"kahoot": QUIZ}}))
        self.assertEqual(len(bot.key.questions), 4)

    async def test_discover_by_uuid_skips_non_quiz_ids(self):
        bot = self.make_bot({"/" + UUID_A: QUIZ})  # UUID_GAME -> 404 (it is a game id, not a quiz)
        bot._ingest(json.dumps({"liveGameId": UUID_GAME, "quizId": UUID_A}))
        await bot._discover()
        self.assertEqual(bot.key.title, "Demo")
        self.assertEqual(len(bot.tried), 2)

    async def test_discover_by_title_needs_unambiguous_match(self):
        cards = lambda *specs: {"entities": [{"card": {"uuid": u, "title": t, "number_of_questions": n}} for u, t, n in specs]}
        routes = {"/": cards((UUID_A, "Demo", 4), ("22222222-2222-4222-8222-222222222222", "Demo", 9)),
                  "/" + UUID_A: QUIZ}
        bot = self.make_bot(routes)
        bot._ingest(json.dumps({"quizTitle": "Demo"}))
        await bot._discover()
        self.assertIsNone(bot.key)  # two quizzes called "Demo" -> ambiguous
        bot = self.make_bot(routes)
        bot.game_total = 4  # the live game has 4 questions -> only one candidate fits
        bot._ingest(json.dumps({"quizTitle": "Demo"}))
        await bot._discover()
        self.assertEqual(bot.key.title, "Demo")


# ---------------------------------------------------------------------------
# End-to-end: real bot + real browser against a fake Kahoot page.
# ---------------------------------------------------------------------------
MOCK_HTML = """<!doctype html><html><body><div id="root"></div><script>
window.clicks = []; window.done = false;
const root = document.getElementById('root');
const ws = new WebSocket('wss://mock.kahoot.test/cometd/123456/token');
const QUESTIONS = [
  {type: 'single', texts: ['', '', '', '']},                       // text hidden -> positional
  {type: 'single', texts: ['Paris', 'London', 'Berlin', 'Madrid']}, // shuffled, text visible
  {type: 'multi',  texts: ['', '', '', '']},
];
const TITLES = __TITLES__;   // question text shown on the device ('' = hidden)
let q = -1, picked = new Set();
function show(i) {
  root.innerHTML = '';
  if (TITLES[i]) {
    const h = document.createElement('h1');
    h.setAttribute('data-functional-selector', 'block-title');
    h.textContent = TITLES[i];
    root.appendChild(h);
  }
  QUESTIONS[i].texts.forEach((t, n) => {
    const b = document.createElement('button');
    b.setAttribute('data-functional-selector', 'answer-' + n);
    b.style.cssText = 'width:100px;height:60px;display:block';
    b.textContent = t;
    b.onclick = (e) => {
      if (!e.isTrusted) return;   // like real Kahoot: synthetic element.click() is ignored
      if (QUESTIONS[i].type === 'single') { window.clicks.push([n]); next(); }
      else { picked.has(n) ? picked.delete(n) : picked.add(n); }
    };
    root.appendChild(b);
  });
  if (QUESTIONS[i].type === 'multi') {
    const s = document.createElement('button');
    s.setAttribute('data-functional-selector', 'multi-select-submit-button');
    s.textContent = 'Submit';
    s.onclick = (e) => { if (e.isTrusted) { window.clicks.push([...picked].sort()); next(); } };
    root.appendChild(s);
  }
}
function next() {
  root.innerHTML = '<p>Answer sent</p>';
  q += 1;
  if (q >= QUESTIONS.length) { window.done = true; return; }
  ws.send('next');   // server replies with the question index, then we render the buttons
}
ws.onmessage = () => setTimeout(() => show(q), 250);
ws.onopen = () => { q = -1; next(); };
</script></body></html>"""


class MiniWSServer:
    """Tiny RFC 6455 text-frame server (stdlib only). Playwright does not emit
    page.on("websocket") for route_web_socket mocks, so the test needs a real socket."""

    GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, on_message):
        self.on_message = on_message  # str -> Optional[str] reply
        self.page_html = ""

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, r, w):
        try:
            req = await r.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:  # e.g. a browser preconnect that sends nothing
            w.close()
            return
        m = re.search(rb"Sec-WebSocket-Key:\s*(\S+)", req, re.I)
        if m is None:  # plain HTTP request -> serve the mock page from the same loopback origin
            body = self.page_html.encode()
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await w.drain()
            w.close()
            return
        key = m.group(1)
        accept = base64.b64encode(hashlib.sha1(key + self.GUID).digest())
        w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        await w.drain()
        try:
            while True:
                hdr = await r.readexactly(2)
                opcode, length = hdr[0] & 0x0F, hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", await r.readexactly(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", await r.readexactly(8))[0]
                mask = await r.readexactly(4)  # browser frames are always masked
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(await r.readexactly(length)))
                if opcode == 8:
                    break
                if opcode == 1:
                    reply = self.on_message(data.decode())
                    if reply is not None:
                        body = reply.encode()
                        head = bytes([0x81, len(body)]) if len(body) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(body))
                        w.write(head + body)
                        await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            w.close()


IMPOSTOR_UUID = "33333333-3333-4333-8333-333333333333"
IMPOSTOR = {  # same first question and question count as QUIZ, different answers / answer counts
    "title": "Impostor",
    "questions": [{"type": "quiz", "question": "Pick C",
                   "choices": [{"answer": "A", "correct": True}, {"answer": "B", "correct": False},
                               {"answer": "C", "correct": False}, {"answer": "D", "correct": False}]}]
    + [{"type": "quiz", "question": f"Other {i}",
        "choices": [{"answer": "x", "correct": True}, {"answer": "y", "correct": False}]} for i in range(3)],
}


async def run_mock_game(bot, titles, extra_content=None):
    """Play the 3-question mock game; returns the buttons the page registered per question."""
    counter = {"index": -1}

    def fake_cometd_server(_msg):  # each "next" from the page -> next question index
        counter["index"] += 1
        content = json.dumps({"questionIndex": counter["index"], "gameBlockType": "quiz", **(extra_content or {})})
        return json.dumps([{"channel": "/service/player", "data": {"id": 2, "content": content}}])

    server = MiniWSServer(fake_cometd_server)
    await server.start()
    # The page is served by the same loopback server as the socket (a route-fulfilled page
    # has no IP, so Chromium's local-network-access check would block the connection).
    server.page_html = (MOCK_HTML
                        .replace("wss://mock.kahoot.test/cometd/123456/token",
                                 f"ws://127.0.0.1:{server.port}/cometd/123456/token")
                        .replace("__TITLES__", json.dumps(titles)))
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await (await browser.new_context()).new_page()
            await bot.attach(page)
            tasks = [asyncio.create_task(bot.answer_worker(page)), asyncio.create_task(bot.discover_worker())]
            await page.goto(f"http://127.0.0.1:{server.port}/")
            await page.wait_for_function("window.done === true", timeout=20_000)
            clicks = await page.evaluate("window.clicks")
            for t in tasks:
                t.cancel()
            await browser.close()
    finally:
        await server.stop()
    return clicks


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_full_flow_with_known_key(self):
        bot = kb.KahootBot(kb.parse_quiz(QUIZ), fallback="skip", delay=0, debug_path=None)
        clicks = await run_mock_game(bot, ["", "", ""])
        # Q1 positional (C=2), Q2 by text despite shuffle (London=button 1), Q3 multi-select + submit.
        # The page ignores untrusted clicks, so this also proves real mouse clicks are used.
        self.assertEqual(clicks, [[2], [1], [0, 2]])
        # The question index must have come from the WebSocket frames, not just the fallback
        # counter (which would give the same answers in this mock).
        self.assertEqual(bot.ws_index, 2)

    async def test_identifies_quiz_by_itself_at_first_question(self):
        """No key given: the bot searches Kahoot for the question on screen, rejects a quiz that
        shares that question but not the game's answer-count fingerprint, and answers Q1 already
        from the right quiz."""
        cards = {"entities": [{"card": {"uuid": IMPOSTOR_UUID, "title": "Impostor", "number_of_questions": 4}},
                              {"card": {"uuid": UUID_A, "title": "Demo", "number_of_questions": 4}}]}
        bot = kb.KahootBot(None, fallback="skip", delay=0, debug_path=None, nickname="Robo")
        bot.request = FakeRequest({"/": cards, "/" + IMPOSTOR_UUID: IMPOSTOR, "/" + UUID_A: QUIZ})
        clicks = await run_mock_game(bot, ["Pick C", "Capital of the UK?", "Pick the vowels"],
                                     {"quizQuestionAnswers": [4, 4, 4, 2], "totalGameBlockCount": 4})
        self.assertEqual(bot.key.title, "Demo")
        self.assertEqual(clicks, [[2], [1], [0, 2]])


class Verification(unittest.TestCase):
    def test_fingerprint(self):
        key = kb.parse_quiz(QUIZ)
        self.assertEqual(kb.fingerprint_score(key, [4, 4, 4, 2]), 1.0)
        self.assertLess(kb.fingerprint_score(kb.parse_quiz(IMPOSTOR), [4, 4, 4, 2]), 0.9)
        self.assertEqual(kb.fingerprint_score(key, [4, 4]), 0.0)  # different length

    def test_verify_rules(self):
        bot = kb.KahootBot(None, "skip", 0, None)
        key = kb.parse_quiz(QUIZ)
        self.assertEqual(bot._verify(key), (True, ""))  # nothing known yet: not contradicted, not confirmed
        bot.game_total = 4
        self.assertEqual(bot._verify(key, "capital of the uk")[1], "contains this question + same question count")
        bot.game_total = 9
        self.assertFalse(bot._verify(key)[0])  # question count contradicts
        bot.game_total, bot.fingerprint = None, [4, 4, 4, 2]
        self.assertEqual(bot._verify(key)[1], "answer-count fingerprint")
        bot.seen_questions = ["Completely different question"]
        self.assertFalse(bot._verify(key)[0])  # text on screen contradicts

    def test_page_text_candidates(self):
        text = "\n".join(["Robo", "1 of 4", "Which of these cities is the capital of the UK?", "London", "Paris"])
        self.assertEqual(kb.page_text_candidates(text, ["London", "Paris"], "Robo"),
                         ["Which of these cities is the capital of the UK?"])


if __name__ == "__main__":
    unittest.main()
