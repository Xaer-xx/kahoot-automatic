# Kahoot automatický hráč

Pripojí sa do hry Kahoot v skutočnom prehliadači (Playwright), sleduje WebSocket komunikáciu hry aj stránku
a v okamihu, keď sa objavia tlačidlá odpovedí, klikne na správnu odpoveď.

> **Najprv si prečítajte.** Kahoot hráčovi nikdy neposiela správnu odpoveď pred tým, než odpovie, takže
> riešenie sa z live hry nedá "prečítať". Skript preto potrebuje **kľúč odpovedí**, ktorý si zostaví zo samotného
> kvízu cez verejné API Kahootu. Funguje to pre každý verejný kvíz (alebo váš vlastný, ktorý ste zverejnili);
> kvíz zadáte odkazom, UUID alebo názvom. Bez kľúča vie len hádať.
>
> Používanie botov v cudzích hrách porušuje podmienky služby Kahootu a je nespravodlivé voči ostatným hráčom.
> Najlepšie je používať ho na vlastných kvízoch / testovacích hrách.

## 1. Predpoklady

| Požiadavka | Poznámka |
|---|---|
| Python **3.10+** | `python --version` |
| Internet | kahoot.it, API kvízov a jednorazové stiahnutie prehliadača |
| ~200 MB miesta | Chromium od Playwrightu |

## 2. Inštalácia (Windows PowerShell; na macOS/Linux rovnaké príkazy)

```powershell
cd C:\Users\Dell\Desktop\kahoot

# voliteľné, ale odporúčané: izolované prostredie
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

## 3. Spustenie

```powershell
python kahoot_bot.py
```

Skript sa opýta na **PIN hry** (len číslice), **prezývku** (max. 15 znakov) a **kvíz**.
Pri otázke na kvíz stlačte **Enter** a bot si kvíz zistí sám (pozri nižšie). Ak kvíz poznáte, môžete vložiť
odkaz `create.kahoot.it/details/...`, UUID alebo časť názvu.
Alebo všetko zadajte cez parametre:

```powershell
python kahoot_bot.py --pin 1234567 --name Robo --quiz "https://create.kahoot.it/details/xyz/7c4ef3fe-7266-4a6b-861b-15719738ad2c"
```

| Parameter | Význam |
|---|---|
| `--quiz` | Odkaz, UUID alebo názov kvízu pre kľúč. `""` = zistiť automaticky |
| `--max-wait 3` | Koľko sekúnd najviac môže otázka čakať, kým sa kvíz identifikuje |
| `--solver claude\|none` | Posledná možnosť, keď sa kvíz nenájde: opýtať sa Claude (treba `ANTHROPIC_API_KEY`) |
| `--fallback random\|skip` | Čo robiť, keď sa v kľúči nenájde otázka (predvolené `random`) |
| `--delay 1.5` | Počkať toľko sekúnd pred kliknutím (predvolené 0 = čo najrýchlejšie) |
| `--headless` | Skryť okno prehliadača (predvolene sa okno zobrazuje, je menej pravdepodobné, že ho Kahoot zablokuje) |
| `--browser` | `chromium` (predvolené), `firefox`, `webkit` |
| `--debug` | Podrobný log a zápis všetkých WebSocket rámcov + DOM udalostí do `kahoot_debug.jsonl` |

Ukončenie: **Ctrl+C** alebo zatvorenie okna prehliadača. Návratový kód `1` znamená, že pripojenie zlyhalo
(napr. „We didn't recognize that game PIN").

## 4. Ako to funguje

1. **Kľúč odpovedí** - `GET https://create.kahoot.it/rest/kahoots/<uuid>` vráti všetky otázky s príznakom
   `correct` pri každej možnosti. Bez `--quiz` si bot nájde kvíz sám, podobne ako online „answer solvery“:
   - prehľadá všetko, čo Kahoot hráčovi pošle (odpoveď pri pripojení, správy zo socketu), či tam nie je ID alebo text kvízu;
   - hľadá v Kahoote podľa tohto textu a podľa **otázky zobrazenej na obrazovke** (vyhľadávanie Kahootu
     indexuje aj texty otázok, takže nájde aj súkromnú kópiu verejného kvízu);
   - každý nájdený kvíz pred použitím **overí**: hra posiela každému hráčovi `quizQuestionAnswers`, teda počet
     možností pri každej otázke (napr. `[4,4,2,4,...]`), čo kvíz takmer jednoznačne identifikuje.
     Kvíz, ktorý má len rovnakú otázku, sa odmietne.
   - Prvá otázka na to môže čakať najviac `--max-wait` sekúnd, potom je každý klik okamžitý.
2. **Pripojenie** - otvorí kahoot.it, zavrie cookie lištu, zadá PIN a prezývku. Chyby, ktoré Kahoot zobrazí
   (zlý PIN, zamknutá hra, odmietnutá prezývka), sa prečítajú zo stránky a ihneď nahlásia.
3. **Inšpekcia paketov** - hra používa CometD WebSocket (`wss://kahoot.it/cometd/<pin>/<token>`).
   `ws.on("framereceived")` v Playwrighte poskytne každý rámec: JSON pole správ, napr.
   `{"channel":"/service/player","data":{"id":2,"content":"{\"questionIndex\":3,...}"}}`.
   `content` je JSON *reťazec*. Skript z neho číta `questionIndex` (ktorá položka kľúča platí) a
   `isCorrect` (aby zalogoval, ako sa darilo).
4. **Sledovanie DOM** - vložený `MutationObserver` zavolá Python v okamihu, keď sa tlačidlá
   `answer-0 … answer-3` zobrazia. Žiadne pollovanie, teda žiadne oneskorenie.
5. **Odpovedanie** - vyberie tlačidlo správnej možnosti (podľa *textu* odpovede, ak ho stránka zobrazuje, čo
   prežije miešanie odpovedí; inak podľa pozície) a stlačí ho skutočným klikom myši (Kahoot ignoruje
   syntetické `element.click()`). Bežný čas od zobrazenia tlačidiel po klik: 30-60 ms.

**Kedy je úspešnosť 100 %?** Keď sa kvíz podarí identifikovať (verejný kvíz alebo jeho kópia), každá
podporovaná otázka sa zodpovie podľa skutočného kľúča. Ak je kvíz súkromný bez verejného originálu **a**
učiteľ nezobrazuje otázky na zariadeniach hráčov, obrazovka hráča obsahuje len farebné tvary. Vtedy odpoveď
nevie zistiť žiadny nástroj a bot použije Claude (potrebuje text) alebo tip.

Poradie párovania otázok: text otázky zo stránky > index z WebSocketu > bežiaci počítadlo.

## 5. Podporované / nepodporované

Podporované: výber jednej odpovede (vrátane pravda/nepravda) a výber viacerých odpovedí.
Nepodporované (preskočí sa alebo sa použije náhodný tip): jumble, posuvník, písanie odpovede, ankety, word cloud, puzzle.
Tímový režim, dvojfázové overenie a generátory mien nie sú ošetrené.

## 6. Testy

```powershell
python -m unittest -v test_kahoot_bot
```

Pokrývajú logiku parsovania a rozhodovania a tiež end-to-end beh skutočného bota proti simulovanej stránke Kahootu
so skutočným lokálnym WebSocket serverom (jedna odpoveď, premiešané odpovede s textom, viac odpovedí).

## 7. Čo bolo a čo nebolo overené

Overené proti skutočnej službe: selektory úvodnej stránky (pole PIN, tlačidlo Join, cookie lišta), chybová
cesta pri zlom PIN-e, API na vyhľadanie/stiahnutie kvízu a jeho formát JSON.

**Neoverené**, pretože existujú len v živej hre: selektory obrazovky s prezývkou, selektory tlačidiel odpovedí
(`data-functional-selector="answer-N"`), tlačidlo odoslania pri viacerých odpovediach a presné polia v živých
WebSocket správach. Všetky sú definované ako konštanty na začiatku `kahoot_bot.py`.

Ak sa bot pripojí, ale neklikne:

1. Spustite s `--debug` a prejdite jednu otázku.
2. Otvorte `kahoot_debug.jsonl`. Riadky `dom_event` ukazujú, čo videl sledovač stránky; riadky `ws_frame` surové rámce.
3. Upravte `SEL_ANSWER_PREFIX` / `SEL_NICK_INPUT` atď., prípadne názvy polí v `_on_player_message`.

Debug súbor obsahuje komunikáciu vašej relácie; nezverejňujte ho.

Známe obmedzenie: ak hostiteľ mieša odpovede **a** obrazovka hráča zobrazuje len farebné tvary (bez textu),
pozície tlačidiel nemusia zodpovedať poradiu možností v kľúči a bot klikne na zlú farbu. Odhalia to
logy `isCorrect` a upozornenie „2 wrong in a row".
