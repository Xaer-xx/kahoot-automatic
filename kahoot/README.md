# Kahoot auto-player

Joins a Kahoot game in a real browser (Playwright), watches the game's WebSocket traffic and the page,
and clicks the correct answer the instant the answer buttons appear.

> **Read this first.** Kahoot never sends a player the correct answer before they answer, so the live game
> cannot be "read" for the solution. The script needs an **answer key**, which it builds from the quiz itself
> through Kahoot's public API. That works for any quiz that is public (or that you own and have made public);
> you point it at the quiz by link, UUID or title. With no key it can only guess.
>
> Using bots in other people's games breaks Kahoot's terms of service and is unfair to the other players.
> This is best used on your own quizzes / test games.

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Python **3.10+** | `python --version` |
| Internet access | for kahoot.it, the quiz API, and the one-time browser download |
| ~200 MB disk | Playwright's Chromium build |

## 2. Install (Windows PowerShell; same commands on macOS/Linux)

```powershell
cd C:\Users\Dell\Desktop\kahoot

# optional but recommended: isolated environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

## 3. Run

```powershell
python kahoot_bot.py
```

It prompts for the **Game PIN** (digits only), a **nickname** (max 15 characters) and the **quiz**.
Press **Enter** at the quiz prompt to let the bot identify the quiz by itself (see below), or paste the
`create.kahoot.it/details/...` link / UUID / part of the title if you know it.
Or pass everything as flags:

```powershell
python kahoot_bot.py --pin 1234567 --name Robo --quiz "https://create.kahoot.it/details/xyz/7c4ef3fe-7266-4a6b-861b-15719738ad2c"
```

| Flag | Meaning |
|---|---|
| `--quiz` | Link, UUID or title used for the answer key. `""` = identify automatically |
| `--max-wait 3` | Max seconds a question may wait while the quiz is being identified |
| `--solver claude\|none` | Last resort when no quiz is found: ask Claude (needs `ANTHROPIC_API_KEY`) |
| `--fallback random\|skip` | What to do when no key entry applies (default `random`) |
| `--delay 1.5` | Wait this many seconds before clicking (default 0 = as fast as possible) |
| `--headless` | Hide the browser window (headed is the default and less likely to be blocked) |
| `--browser` | `chromium` (default), `firefox`, `webkit` |
| `--debug` | Verbose log, and write every WebSocket frame + DOM event to `kahoot_debug.jsonl` |

Stop with **Ctrl+C** or by closing the browser window. Exit code `1` means the join failed
(e.g. "We didn't recognize that game PIN").

## 4. How it works

1. **Answer key** - `GET https://create.kahoot.it/rest/kahoots/<uuid>` returns every question with a
   `correct` flag on each choice. Without `--quiz` the bot finds the quiz itself, like online answer
   solvers do:
   - it scans everything Kahoot sends the player (join response, socket messages) for a quiz id or text;
   - it searches Kahoot for that text and for the **question shown on the screen** (Kahoot's search
     indexes question texts, so even a private copy of a public quiz is found);
   - every search hit is **verified** before use: the game sends each player `quizQuestionAnswers`, the
     number of choices of every question (e.g. `[4,4,2,4,...]`), which identifies a quiz almost uniquely.
     A quiz that merely shares a question is rejected.
   - The first question waits up to `--max-wait` seconds for this; after that every click is instant.
2. **Join** - opens kahoot.it, dismisses the cookie banner, enters PIN and nickname. Errors that Kahoot
   shows (bad PIN, locked game, rejected nickname) are read from the page and reported immediately.
3. **Packet inspection** - the game uses a CometD WebSocket (`wss://kahoot.it/cometd/<pin>/<token>`).
   Playwright's `ws.on("framereceived")` gives each frame: a JSON array of messages, e.g.
   `{"channel":"/service/player","data":{"id":2,"content":"{\"questionIndex\":3,...}"}}`.
   `content` is a JSON *string*. The script reads `questionIndex` (which key entry applies) and
   `isCorrect` (to log how it did).
4. **DOM monitoring** - an injected `MutationObserver` calls back into Python the moment the
   `answer-0 … answer-3` buttons become visible. No polling, so there is no polling delay.
5. **Answering** - picks the button for the correct choice (by the answer *text* if the page shows it, which
   survives Kahoot's answer shuffling; otherwise by position) and presses it with a real mouse click
   (Kahoot ignores synthetic `element.click()`). Typical time from buttons appearing to click: 30-60 ms.

**When is it 100 %?** When the quiz can be identified (public quiz, or a copy of one), every supported
question is answered from the real answer key. If the quiz is private with no public original **and** the
host does not show questions on players' devices, the player's screen contains nothing but coloured
shapes - no tool can know the answer then, and the bot falls back to Claude (needs the text) or a guess.

Question matching order: question text shown on the page > index from the WebSocket > running counter.

## 5. Supported / unsupported

Supported: single-choice (including true/false) and multiple-select.
Not supported (skipped or random fallback): jumble, slider, type-answer, polls, word cloud, puzzle.
Teams mode, 2-step authentication and name generators are not handled.

## 6. Tests

```powershell
python -m unittest -v test_kahoot_bot
```

Covers the parsing/decision logic, plus an end-to-end run of the real bot against a mock Kahoot page with a
real local WebSocket server (single choice, shuffled answers with text, multi-select).

## 7. What has and has not been verified

Verified against the real service: the join page selectors (PIN field, Join button, cookie banner), the
bad-PIN error path, the quiz search/fetch API and its JSON format.

**Not** verified, because they only exist inside a live game: the nickname screen selectors, the in-game
answer-button selectors (`data-functional-selector="answer-N"`), the multi-select submit button, and the
exact fields in the live WebSocket messages. All of them are defined as constants at the top of
`kahoot_bot.py`.

If the bot joins but does not click:

1. Run with `--debug` through one question.
2. Open `kahoot_debug.jsonl`. `dom_event` lines show what the page watcher saw; `ws_frame` lines show the raw frames.
3. Adjust `SEL_ANSWER_PREFIX` / `SEL_NICK_INPUT` / etc., or the field names in `_on_player_message`.

The debug file contains your session's socket traffic; don't post it publicly.

Known limitation: if the host shuffles answers **and** the player screen shows only coloured shapes (no
text), the button positions may not match the key's choice order and the bot will click the wrong colour.
The `isCorrect` log lines and the "2 wrong in a row" warning will reveal this.
