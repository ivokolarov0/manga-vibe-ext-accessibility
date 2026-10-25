"""Manga Accessibility: manga pages described and read out, for readers who are blind or have low vision.

Copy this file into MangaVibe's plugins folder, then Settings -> Plugins -> Reload plugins.
The only other thing it needs is Ollama with a model that can see pictures (e.g. gemma4), on any
computer in the home network (see README.md).

How it works: while you read with "Describe pages" switched on, the reader asks for each page. A page
that isn't described yet goes to the front of the queue, with the next few pages behind it. One thread
sends the pages to Ollama, which answers, for each panel in reading order: what it shows, and every
piece of text with who says it (sound effects explained). The answer is saved per page in MangaVibe's
database, so each page is described only once.

What the reader gets:
  - `speech`: the page as plain text in reading order. MangaVibe reads it aloud ("Read aloud" in the
    reader options) and hands it to screen readers (TalkBack, NVDA, VoiceOver). Needs MangaVibe with
    plugin API 5.
  - `note`: a one-line summary of the page in the top bar
Nothing is drawn over the page itself.

The characters the model names are kept per series (plugin API 4), and every page is described with
that list and the summary of the page before, so the same character keeps the same name.
"""
import base64
import functools
import heapq
import io
import itertools
import json
import re
import threading
import time

import httpx

from app.models import Book, Series
from app.plugins import Action, Overlay, Plugin, SettingDef

FIELDS = getattr(Overlay, "__dataclass_fields__", {})
HAS_NOTE = "note" in FIELDS        # plugin API 4: a quiet line in the reader's top bar
HAS_SPEECH = "speech" in FIELDS    # plugin API 5: text the reader reads aloud

VERSION = "0.2.0"
DATA_VERSION = 1          # the format of what is saved per page
MAX_SIDE = 1600           # pages are shrunk so their longest side is at most this, before they go to Ollama
OFFLINE_RETRY = 15        # seconds between tries while Ollama can't be reached
PAGE_TRIES = 2            # a page that fails this often is saved as failed (not tried forever)
MAX_TOKENS = 3000         # longest answer: a busy page needs about 1500; a model stuck repeating itself stops here
WAIT = 600                # seconds to wait for an answer (Ollama may be busy with another program first)
MAX_CHARACTERS = 40       # characters kept per series

# queue priorities: lower goes first
NOW, AHEAD, CHAPTER, SERIES = 0, 1, 2, 3

KINDS = ("speech", "thought", "narration", "sign", "sound")


class MangaAccessibility(Plugin):
    id = "manga-accessibility"
    name = "Manga Accessibility"
    version = VERSION
    description = ("Describes each page for readers who are blind or have low vision: what every panel "
                   "shows, who says what, and the sound effects. MangaVibe reads it aloud. Runs on your "
                   "own computer with Ollama.")
    repo = "ivokolarov0/manga-vibe-ext-accessibility"
    permissions = ["Reads page images", "Sends page images to the Ollama server you set (your computer)"]
    overlay_label = "Describe pages"
    settings_fields = [
        SettingDef("ollama_url", "Ollama server",
                   help="The address of the computer that runs Ollama, e.g. http://192.168.1.50:11434"),
        SettingDef("model", "Model", default="gemma4",
                   help="A model that can see pictures. gemma4 (8 GB graphics memory) is fast; "
                        "gemma4:12b gets the order of the panels right more often."),
        SettingDef("language", "Language", default="English",
                   help="The descriptions are written in this language, and the text is translated into it"),
        SettingDef("detail", "What is read out", default="full",
                   options=[("full", "Everything: what each panel shows, and who says what"),
                            ("text", "Only the text: who says what")]),
        SettingDef("ahead", "Pages to describe ahead", default="4",
                   help="Pages after the one you're reading that are described in advance"),
    ]
    global_actions = [Action("test", "Test connection"), Action("stop", "Stop the queue")]
    actions = [Action("series", "Describe the whole series"),
               Action("forget", "Forget this series' descriptions",
                      confirm="Delete the saved descriptions of every book in this series?")]
    book_actions = [Action("chapter", "Describe this chapter"),
                    Action("again", "Describe this chapter again")]
    def __init__(self):
        self.lock = threading.Lock()
        self.heap = []                      # (priority, order, book_id, page)
        self.best = {}                      # (book_id, page) -> best priority queued
        self.busy = set()                   # (book_id, page) taken by the worker, not saved yet
        self.order = itertools.count()
        self.thread = None
        self.offline = ""                   # why Ollama can't be reached, shown in the reader
        self.last = ""                      # last thing done, for the Settings status line
        self.tries = {}                     # (book_id, page) -> failed attempts
        self.current = None                 # (what, since): the page Ollama is working on, for the status

    # --- what MangaVibe asks ------------------------------------------------------------------
    def page_overlay(self, ctx, book, page):
        if not ctx.setting("ollama_url"):
            return Overlay(message="Manga Accessibility: set the Ollama server in Settings → Plugins")
        saved = ctx.page_data(book)
        data = saved.get(page)
        if data is None:
            self.enqueue(ctx, book.id, [page], NOW)
        ahead = _int(ctx.setting("ahead"), 4)
        self.enqueue(ctx, book.id, [p for p in range(page + 1, min(page + 1 + ahead, book.page_count))
                                    if p not in saved], AHEAD)
        if data is None:
            return Overlay(pending=True, message=self.offline or "Describing the page…")
        if data.get("error"):
            return overlay(message=f"Couldn't describe this page: {data['error']}",
                           speech=f"Page {page + 1}. This page couldn't be described.")
        return overlay(note=data.get("summary", ""),
                       speech=speech_for(data, page, ctx.setting("detail")))

    def book_badges(self, ctx, books):
        done = ctx.page_data_counts(books)
        out = {}
        for b in books:
            n = done.get(b.id, 0)
            if not b.page_count:
                continue  # a broken file: nothing to describe
            if n >= b.page_count:
                out[b.id] = "Described"
            elif n:
                out[b.id] = f"Described {n}/{b.page_count}"
        return out

    def run_book_action(self, ctx, key, book):
        if key == "again":
            ctx.clear_page_data(book)
            ctx.db.commit()  # before queueing: the worker must not see the old pages and skip them
        saved = ctx.page_data(book)
        todo = [p for p in range(book.page_count) if p not in saved]
        self.enqueue(ctx, book.id, todo, CHAPTER)
        return f"Describing {len(todo)} pages" if todo else "This chapter is already described"

    def run_action(self, ctx, key, series):
        books = (ctx.db.query(Book).filter(Book.series_id == series.id, Book.hidden.is_(False),
                                           Book.missing_since.is_(None), Book.error.is_(None))
                 .order_by(Book.sort_index).all())
        if key == "forget":
            for b in books:
                ctx.clear_page_data(b)
            if hasattr(ctx, "clear_series_data"):
                ctx.clear_series_data(series)
            return "Descriptions deleted"
        done = ctx.page_data_counts(books)
        count = 0
        for b in books:
            if done.get(b.id, 0) < b.page_count:
                saved = ctx.page_data(b)
                todo = [p for p in range(b.page_count) if p not in saved]
                self.enqueue(ctx, b.id, todo, SERIES)
                count += len(todo)
        return f"Describing {count} pages" if count else "The whole series is already described"

    def run_global_action(self, ctx, key):
        if key == "stop":
            with self.lock:
                n = len(self.best)
                self.heap.clear()
                self.best.clear()
            return f"Removed {n} pages from the queue"
        return test_connection(ctx)

    def status(self, ctx):
        with self.lock:
            waiting = len(self.best)
        parts = [f"Queue: {waiting} pages" if waiting else "Queue empty"]
        current = self.current
        if current and not self.offline:
            secs = int(time.monotonic() - current[1])
            # a long wait is usually Ollama finishing work for another program first (e.g. a translator)
            parts.append(f"{current[0]} ({secs} s{', Ollama may be busy' if secs > 60 else ''})")
        if self.offline:
            parts.append(self.offline)
        elif self.last:
            parts.append(self.last)
        return " · ".join(parts)

    # --- the queue ------------------------------------------------------------------------------
    def enqueue(self, ctx, book_id, pages, priority):
        if not pages:
            return
        with self.lock:
            for p in pages:
                key = (book_id, p)
                if key in self.busy or self.best.get(key, 99) <= priority:
                    continue
                self.best[key] = priority
                # the newest request of the page you're reading goes first; the rest in page order
                order = -next(self.order) if priority == NOW else next(self.order)
                heapq.heappush(self.heap, (priority, order, book_id, p))
            if self.thread is None or not self.thread.is_alive():
                for key in self.busy:  # held by a worker that stopped (plugin switched off and on)
                    self.best[key] = NOW
                    heapq.heappush(self.heap, (NOW, -next(self.order), *key))
                self.busy.clear()
                self.thread = ctx.run_in_thread(self.worker)

    def take(self):
        """The next page to work on, or None."""
        with self.lock:
            while self.heap:
                priority, _, book_id, page = heapq.heappop(self.heap)
                key = (book_id, page)
                if self.best.get(key) != priority:
                    continue  # stale: queued again with a better priority, or the queue was stopped
                del self.best[key]
                self.busy.add(key)
                return key
        return None

    def release(self, key, again=False):
        with self.lock:
            self.busy.discard(key)
            if again and key not in self.best:  # put it back in front (Ollama was away)
                self.best[key] = NOW
                heapq.heappush(self.heap, (NOW, -next(self.order), *key))

    def failed(self, ctx, book, page, key, error):
        """A page went wrong. Try it again later, or save it as failed after PAGE_TRIES."""
        self.tries[key] = self.tries.get(key, 0) + 1
        ctx.log.warning("Page %s of %s failed: %s", page + 1, book.file_name, error)
        if self.tries[key] >= PAGE_TRIES:
            ctx.set_page_data(book, page, {"v": DATA_VERSION, "error": str(error)[:200]})
            ctx.db.commit()
            self.tries.pop(key, None)
            self.release(key)
        else:
            self.release(key, again=True)

    # --- the worker: page -> description (Ollama) ---------------------------------------------------
    def worker(self, ctx):
        while not ctx.should_stop():
            key = self.take()
            if key is None:
                time.sleep(0.3)
                continue
            book_id, page = key
            ctx.db.expire_all()
            book = ctx.db.get(Book, book_id)
            if book is None or ctx.get_page_data(book, page) is not None:
                self.release(key)
                continue
            self.current = (f"describing page {page + 1} of {book.file_name}", time.monotonic())
            try:
                data = self.describe(ctx, book, page, again=key in self.tries)
            except (httpx.ReadTimeout, httpx.WriteTimeout):
                self.failed(ctx, book, page, key, RuntimeError(f"Ollama didn't answer in {WAIT // 60} minutes"))
                continue
            except httpx.TransportError as e:
                self.went_offline(ctx, e)
                self.release(key, again=True)
                continue
            except Exception as e:
                self.failed(ctx, book, page, key, e)
                continue
            finally:
                self.current = None
            self.offline = ""
            ctx.set_page_data(book, page, data)
            ctx.db.commit()  # the reader shows it at its next look
            self.tries.pop(key, None)
            self.release(key)
            self.last = f"last: page {page + 1} of {book.file_name}"

    def describe(self, ctx, book, page, again=False):
        """One page. `again`: it failed before, so the model is asked a little less strictly (a model
        stuck repeating itself usually gets out of it)."""
        series = ctx.db.get(Series, book.series_id)
        characters = known_characters(ctx, series)
        before = ctx.get_page_data(book, page - 1) if page > 0 else None
        picture = to_jpeg(ctx.read_page(book, page))
        order = reading_order(ctx, series)
        prompt = describe_prompt(ctx.setting("language") or "English", series_info(series), characters,
                                 (before or {}).get("summary", ""), order)
        data = clean(ask(ctx, prompt, picture, temperature=0.6 if again else 0.2))
        data["panels"] = order_panels(data["panels"], order)
        data.update(v=DATA_VERSION, model=str(ctx.setting("model") or "gemma4"), order=order)
        remember_characters(ctx, series, characters, data.pop("characters", []))
        return data

    def went_offline(self, ctx, error):
        msg = f"Can't reach Ollama ({_url(ctx.setting('ollama_url'))})"
        if self.offline != msg:
            ctx.log.warning("%s: %s", msg, error)
        self.offline = msg
        _sleep(ctx, OFFLINE_RETRY)


def overlay(**kw):
    """An Overlay with only what this MangaVibe knows: `note` needs plugin API 4, `speech` API 5."""
    if not HAS_NOTE:
        kw.pop("note", None)
    if not HAS_SPEECH:
        kw.pop("speech", None)
    return Overlay(**kw)


# --- helpers ------------------------------------------------------------------------------------------

def _int(value, default):
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _sleep(ctx, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not ctx.should_stop():
        time.sleep(0.5)


def _url(value):
    """http://pc:11434 from what the owner typed: adds http://, drops a trailing / and an OpenAI-style /v1."""
    value = str(value or "").strip().rstrip("/")
    if value and not re.match(r"^https?://", value):
        value = "http://" + value
    return re.sub(r"/v1$", "", value)


def _http(timeout):
    """The HTTP client for Ollama (tests put a fake Ollama here)."""
    return httpx.Client(timeout=timeout)


def to_jpeg(image: bytes, max_side=MAX_SIDE) -> bytes:
    """The page as a JPEG no bigger than max_side (Ollama doesn't read every format, e.g. WebP)."""
    from PIL import Image
    with Image.open(io.BytesIO(image)) as im:
        im = im.convert("RGB")
    scale = min(1.0, max_side / max(im.size))
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


# --- talking to Ollama ------------------------------------------------------------------------------------

BOX = {"type": "array", "items": {"type": "integer"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "panels": {"type": "array", "items": {"type": "object", "properties": {
            "box": BOX,
            "scene": {"type": "string"},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "original": {"type": "string"},
                "kind": {"type": "string", "enum": list(KINDS)},
                "speaker": {"type": "string"},
                "text": {"type": "string"}},
                "required": ["original", "kind", "speaker", "text"]}}},
            "required": ["box", "scene", "lines"]}},
        "summary": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "look": {"type": "string"}},
            "required": ["name", "look"]}},
    },
    "required": ["panels", "summary", "characters"],
}

_NO_THINK = set()  # Ollama servers that refused "think" (older versions): asked once, then left out


def ask(ctx, prompt: str, picture: bytes, temperature=0.2) -> dict:
    """One page to Ollama's own API, with the answer held to SCHEMA. Returns the parsed answer."""
    base = _url(ctx.setting("ollama_url"))
    body = {
        "model": ctx.setting("model") or "gemma4",
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(picture).decode()]}],
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": temperature, "num_predict": MAX_TOKENS},
    }
    if base not in _NO_THINK:
        body["think"] = False  # thinking models: answer straight away
    with _http(httpx.Timeout(WAIT, connect=15)) as http:
        r = http.post(base + "/api/chat", json=body)
        if r.status_code == 400 and "think" in body and "think" in r.text:
            _NO_THINK.add(base)
            del body["think"]
            r = http.post(base + "/api/chat", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Ollama said {r.status_code}: {_error_text(r)}")
        answer = r.json()
        text = answer.get("message", {}).get("content") or ""
    if answer.get("done_reason") == "length":
        raise RuntimeError("The model's answer went on too long (it may have repeated itself)")
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)  # a model that wrapped the JSON in other text
        if not m:
            raise RuntimeError("The model's answer wasn't JSON")
        return json.loads(m.group(0))


def _error_text(r):
    try:
        return str(r.json().get("error") or r.text)[:200]
    except ValueError:
        return r.text[:200]


ORDERS = {
    "rtl": "This is a manga: it reads RIGHT TO LEFT, also when its text is in English. Go through the "
           "panels row by row from the top. In each row, start with the panel on the RIGHT and end with the one "
           "on the left; a tall panel on the right comes before the panels beside it on the left. Inside a "
           "panel, read the bubbles from right to left, top to bottom.",
    "ltr": "This is a comic that reads LEFT TO RIGHT. Go through the panels row by row from the top. In each "
           "row, start with the panel on the LEFT and end with the one on the right. Inside a panel, read the "
           "bubbles from left to right, top to bottom.",
    "vertical": "This is a vertical comic (webtoon): it reads TOP TO BOTTOM. Go through the panels from the "
                "top of the page to the bottom, and the bubbles from top to bottom.",
}


def describe_prompt(language, info, characters, before, order="rtl") -> str:
    known = "\n".join(f"- {c['name']}: {c['look']}" for c in characters) or "(none yet)"
    parts = [
        "You describe manga pages for a reader who is blind, so they can follow the story by listening.",
        "",
        "Reading order: " + ORDERS.get(order, ORDERS["rtl"]),
        "",
        "For every panel, in reading order:",
        "- box: where the panel is: [x1, y1, x2, y2], on a 0-1000 scale of the page's width and height.",
        "- scene: one or two plain sentences on what the panel shows: who is there, what they do, their "
        "faces and the place. Say what matters for the story; don't repeat the dialogue.",
        "- lines: every piece of text in the panel, in reading order:",
        "  - original: the text exactly as written, in its own language and script.",
        "  - kind: speech (a speech bubble), thought (a thought bubble, often cloud-shaped), narration "
        "(a box with the narrator's text), sign (text that is part of the picture, like a sign or a "
        "letter), or sound (a sound effect drawn into the art).",
        "  - speaker: who says or thinks it: the character's name if you know it, otherwise a short "
        "description (\"the boy on the left\"). Empty for narration, signs and sounds.",
        f"  - text: the text in {language}, translated if it's in another language. For a sound, write "
        f"the sound and what it means, e.g. \"BOOM (an explosion)\".",
        "",
        "summary: one sentence on what happens on this page.",
        "characters: the characters on this page whose name you know, each with a short description of "
        "how they look (hair, clothes, age), so they can be recognised on later pages. Only use a name that "
        "is said on the page or that is in the list below; never make one up.",
        "",
        f"Write scene, text, summary and look in {language}.",
    ]
    if info:
        parts += ["", "About the series:", info]
    parts += ["", "Characters known so far:", known]
    if before:
        parts += ["", f"The page before: {before}"]
    parts += ["", "Answer with JSON only."]
    return "\n".join(parts)


def series_info(series) -> str:
    if series is None:
        return ""
    lines = [f"Title: {series.title}"] if getattr(series, "title", None) else []
    desc = re.sub(r"<[^>]+>", " ", str(getattr(series, "description", "") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()
    if desc:
        lines.append(f"Story: {desc[:1200]}")
    return "\n".join(lines)


# words that make a "name" a description: "person on the left", "the tall boy", "unknown"
NOT_A_NAME = re.compile(r"\b(left|right|top|bottom|middle|person|people|man|woman|boy|girl|guy|kid|child|"
                        r"someone|somebody|unknown|narrator|character|figure|creature|voice|everyone|crowd|"
                        r"speaker|other|another)\b", re.I)


def is_name(name) -> bool:
    name = str(name or "").strip()
    return bool(name) and len(name) <= 40 and not NOT_A_NAME.search(name) \
        and not name.casefold().startswith(("the ", "a ", "an "))


def known_characters(ctx, series) -> list:
    if series is None or not hasattr(ctx, "get_series_data"):
        return []
    found = ctx.get_series_data(series, "characters")
    return [c for c in found if isinstance(c, dict) and is_name(c.get("name"))] if isinstance(found, list) else []


def reading_order(ctx, series) -> str:
    """rtl, ltr or vertical: the series' reading direction in MangaVibe, the same the reader uses (set on
    the series, or from its metadata: a manga from Japan is right to left, a manhwa or comic left to right;
    else its library folder's direction). Older MangaVibe (before plugin API 5) doesn't work it out: then
    the series' own direction, or its metadata, or manga."""
    if series is None:
        return "rtl"
    if hasattr(ctx, "reading_direction"):
        mode = ctx.reading_direction(series)
    elif getattr(series, "reader_mode", None):
        mode = series.reader_mode
    elif getattr(series, "country_of_origin", None) or getattr(series, "format", None):
        mode = "rtl" if str(series.country_of_origin or "").upper() == "JP" else "ltr"
    else:
        mode = "rtl"
    return mode if mode in ORDERS else "rtl"


def order_panels(panels, order) -> list:
    """Panels in reading order, by where they are on the page: the model is told the order, but a small
    one doesn't always keep to it. Rows from the top; in a row right to left (manga) or left to right.
    Panels that are above each other in the same column (e.g. beside a tall panel) go top to bottom.
    Left as the model gave them when a panel has no place."""
    if len(panels) < 2 or any(not p.get("box") for p in panels):
        return panels

    def overlap(a1, a2, b1, b2):
        return max(0.0, min(a2, b2) - max(a1, b1))

    if order == "vertical":
        return sorted(panels, key=lambda p: p["box"][1] + p["box"][3] / 2)

    # rows: panels whose heights overlap by at least half of the shorter one
    rows = []
    for p in sorted(panels, key=lambda p: p["box"][1] + p["box"][3] / 2):
        x, y, w, h = p["box"]
        row = rows[-1] if rows else None
        if row and overlap(row["top"], row["bottom"], y, y + h) >= 0.5 * min(h, row["bottom"] - row["top"]):
            row["panels"].append(p)
            row["top"], row["bottom"] = min(row["top"], y), max(row["bottom"], y + h)
        else:
            rows.append({"top": y, "bottom": y + h, "panels": [p]})

    sign = -1 if order == "rtl" else 1

    def before(a, b):
        ax, ay, aw, ah = a["box"]
        bx, by, bw, bh = b["box"]
        if overlap(ax, ax + aw, bx, bx + bw) >= 0.5 * min(aw, bw):  # one column: top first
            return -1 if ay + ah / 2 < by + bh / 2 else 1
        return -1 if sign * (ax + aw / 2) < sign * (bx + bw / 2) else 1

    return [p for row in rows for p in sorted(row["panels"], key=functools.cmp_to_key(before))]


def remember_characters(ctx, series, known, seen):
    """Add the characters the model named on this page to the series' list (newest last, at most
    MAX_CHARACTERS), so later pages use the same names."""
    if series is None or not hasattr(ctx, "set_series_data"):
        return
    by_name = {c["name"].casefold(): c for c in known}
    changed = False
    for c in seen if isinstance(seen, list) else []:
        if not isinstance(c, dict):
            continue
        name, look = str(c.get("name") or "").strip(), str(c.get("look") or "").strip()
        if not is_name(name):
            continue  # "the boy on the left" is not a name
        old = by_name.get(name.casefold())
        if old is None:
            by_name[name.casefold()] = {"name": name, "look": look[:200]}
            changed = True
        elif look and not old.get("look"):
            old["look"] = look[:200]
            changed = True
    if changed:
        ctx.set_series_data(series, "characters", list(by_name.values())[-MAX_CHARACTERS:])


# --- what is saved, and what the reader gets ---------------------------------------------------------------

def _box(value):
    """[x1, y1, x2, y2] on the 0-1000 scale -> (x, y, w, h) fractions, or None if it isn't a usable box.
    A model that answered in fractions (0-1) is understood too."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    scale = 1.0 if max(x1, y1, x2, y2) <= 1.0 else 1000.0
    x1, x2 = sorted((min(max(x1 / scale, 0), 1), min(max(x2 / scale, 0), 1)))
    y1, y2 = sorted((min(max(y1 / scale, 0), 1), min(max(y2 / scale, 0), 1)))
    if x2 - x1 < 0.01 or y2 - y1 < 0.01:
        return None
    return [round(x1, 4), round(y1, 4), round(x2 - x1, 4), round(y2 - y1, 4)]


def _text(value, limit=600):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def clean(answer) -> dict:
    """The model's answer, kept to what we use: {"panels": [{"box", "scene", "lines": [{"kind",
    "speaker", "text", "original"}]}], "summary", "characters"}. A panel's box is (x, y, w, h) fractions,
    used only to put the panels in reading order."""
    if not isinstance(answer, dict):
        raise RuntimeError("The model's answer wasn't a description")
    panels = []
    for p in answer.get("panels") or []:
        if not isinstance(p, dict):
            continue
        lines = []
        for line in p.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = _text(line.get("text"))
            if not text:
                continue
            kind = line.get("kind") if line.get("kind") in KINDS else "speech"
            lines.append({"kind": kind, "speaker": _text(line.get("speaker"), 80) if kind in ("speech", "thought") else "",
                          "text": text, "original": _text(line.get("original"))})
        scene = _text(p.get("scene"))
        if scene or lines:
            panels.append({"box": _box(p.get("box")), "scene": scene, "lines": lines})
    return {"panels": panels, "summary": _text(answer.get("summary"), 300),
            "characters": answer.get("characters") or []}


SAYS = {"speech": "says", "thought": "thinks"}


def speech_for(data, page, detail="full") -> str:
    """The page as text to listen to, in reading order. "Page 5." first, so a listener knows where they are.

    Page 5.
    Panel 1. Two boys stand in the rain.
    The boy on the right says: Hey, are you listening?
    It's raining again.                 (the same speaker, the next bubble: not named again)
    Sound: BOOM (an explosion).
    """
    out = [f"Page {page + 1}."]
    panels = data.get("panels") or []
    full = detail != "text"
    last = None  # who spoke the line just before: the same speaker again isn't named again
    for n, p in enumerate(panels, 1):
        if full:
            head = f"Panel {n}." if len(panels) > 1 else ""
            out.append(" ".join(x for x in (head, _sentence(p.get("scene"))) if x))
            last = None  # after a description, say who speaks again
        for line in _joined(p.get("lines") or []):
            who = (line.get("kind"), line.get("speaker", "").casefold()) if line.get("speaker") else None
            out.append(_sentence(line.get("text")) if who and who == last else _say_line(line))
            last = who
    if len(out) == 1:
        out.append("No text on this page." if not full else "Nothing to describe on this page.")
    return "\n".join(x for x in out if x)


def _joined(lines) -> list:
    """Signs and sounds next to each other as one line, each text once: "Written: Oden, Beef, Udon."
    rather than a list of every label on a food stall."""
    out = []
    for line in lines:
        kind = line.get("kind")
        prev = out[-1] if out else None
        if kind in ("sign", "sound") and prev and prev.get("kind") == kind:
            text = line.get("text", "").rstrip(".")
            if text.casefold() not in {t.casefold() for t in prev["texts"]}:
                prev["texts"].append(text)
                prev["text"] = ", ".join(prev["texts"])
            continue
        out.append({**line, "texts": [line.get("text", "").rstrip(".")]} if kind in ("sign", "sound") else line)
    return out


def _say_line(line) -> str:
    text, kind, who = line.get("text", ""), line.get("kind"), line.get("speaker", "")
    if kind in SAYS:
        who = who[:1].upper() + who[1:] if who else "Someone"
        return f"{who} {SAYS[kind]}: {_sentence(text)}"
    label = {"narration": "Narration", "sign": "Written", "sound": "Sound"}.get(kind, "Text")
    return f"{label}: {_sentence(text)}"


def _sentence(text) -> str:
    """Ends with a full stop (or ! ?), so a speech engine pauses between lines."""
    text = str(text or "").strip()
    return text if not text or text[-1] in ".!?…。！？\"'”’" else text + "."


def test_connection(ctx) -> str:
    base = _url(ctx.setting("ollama_url"))
    if not base:
        return "Set the Ollama server first"
    model = str(ctx.setting("model") or "gemma4")
    try:
        with _http(15) as http:
            r = http.get(base + "/api/tags")
            r.raise_for_status()
            names = {m.get("name", "") for m in r.json().get("models", [])}
            if model not in names and f"{model}:latest" not in names:
                return f"Ollama answers, but has no model called {model}. On that computer, run: ollama pull {model}"
            r = http.post(base + "/api/show", json={"model": model})
            caps = r.json().get("capabilities") if r.status_code == 200 else None
    except httpx.TransportError as e:
        return f"Can't reach Ollama at {base}: {type(e).__name__}"
    except httpx.HTTPStatusError as e:
        return f"Ollama answered with an error: {e.response.status_code}"
    if caps is not None and "vision" not in caps:
        return f"{model} can't see pictures. Use a model like gemma4"
    return f"Connected: {model} is ready"
