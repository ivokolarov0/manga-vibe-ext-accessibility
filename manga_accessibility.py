"""Manga Accessibility: manga pages described and read out, for readers who are blind or have low vision.

Copy this file into MangaVibe's plugins folder, then Settings -> Plugins -> Reload plugins.
The only other thing it needs is Ollama with a model that can see pictures (e.g. gemma4), on any
computer in the home network (see README.md).

How it works: while you read with "Describe pages" switched on, the reader asks for each page. A page
that isn't described yet goes to the front of the queue, with the next few pages behind it. One thread
sends the pages to Ollama, which answers, for each panel in reading order: what it shows, and every
piece of text with who says it (sound effects explained). The panels are found in the picture first
(from the gutters between them) and numbered on it, so the model describes the real panels. The answer is saved per page in MangaVibe's
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

VERSION = "0.6.0"
DATA_VERSION = 1          # the format of what is saved per page
MAX_SIDE = 1600           # pages are shrunk so their longest side is at most this, before they go to Ollama
OFFLINE_RETRY = 15        # seconds between tries while Ollama can't be reached
PAGE_TRIES = 2            # a page that fails this often is saved as failed (not tried forever)
MAX_TOKENS = 3000         # longest answer: a busy page needs about 1500; a model stuck repeating itself stops here
WAIT = 600                # seconds to wait for an answer (Ollama may be busy with another program first)
MAX_CHARACTERS = 40       # characters kept per series

# queue priorities: lower goes first
NOW, AHEAD, CHAPTER, SERIES = 0, 1, 2, 3

KINDS = ("speech", "thought", "narration", "sign", "sound", "free")  # free: text without a bubble
PLACES = ("right", "left", "top", "bottom", "middle")  # where a bubble is in its panel


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
                   help="A model that can see pictures. gemma4 (8 GB graphics memory) is fast; gemma4:12b "
                        "(12 GB) makes fewer mistakes; gemma4:26b (16 GB) makes the fewest."),
        SettingDef("language", "Language", default="English",
                   help="The descriptions are written in this language, and the text is translated into it"),
        SettingDef("detail", "What is read out", default="full",
                   options=[("full", "Everything: what each panel shows, and who says what"),
                            ("text", "Only the text: who says what"),
                            ("scene", "Only the pictures: what each panel shows, without the text")]),
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
        order = reading_order(ctx, series)
        picture, frames = prepare(ctx.read_page(book, page), order)
        prompt = describe_prompt(ctx.setting("language") or "English", series_info(series), characters,
                                 (before or {}).get("summary", ""), order, len(frames))
        data = clean(ask(ctx, prompt, picture, temperature=0.6 if again else 0.2, numbered=bool(frames)))
        data["panels"] = panels_for(data["panels"], frames, order)
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


# --- finding the panels ----------------------------------------------------------------------------------
# A small model doesn't see where one panel ends: it splits a wide panel in two, or makes a panel of the
# text in a corner. So the panels are found in the picture itself, from the gutters between them (white
# or black lines across the page), outlined and numbered on the picture the model gets, and the model
# describes those. A page whose panels can't be found this way (slanted gutters, art across the whole
# page) is described by the model alone.

WHITE, BLACK = 215, 60    # a gutter's pixels are lighter than WHITE, or darker than BLACK
NOISE = 0.015             # how much else a gutter line may have on it (dust, a bubble across it)
GUTTER = 0.003            # a white gutter is at least this wide (part of the page's longest side)
BLACK_GUTTER = 0.008      # a black gutter at least this wide: a thinner black line is a frame's border
MIN_PANEL = 0.015         # a piece smaller than this part of the page is no panel
MAX_FRAMES = 12           # more than this on a page: the gutters were misread


def find_frames(im) -> list:
    """The panels of a page (a PIL image) as (x, y, w, h) fractions, in no particular order: the page cut
    along its gutters, and each part cut again, until no gutter is left in it."""
    from PIL import Image
    g = im.convert("L")
    W, H = g.size
    marks = {"w": g.point(lambda v: 255 if v < WHITE else 0),   # what isn't white
             "b": g.point(lambda v: 255 if v > BLACK else 0)}   # what isn't black
    run_w = max(2, round(GUTTER * max(W, H)))
    run_b = max(3, round(BLACK_GUTTER * max(W, H)))

    def gutters(r, across):
        """[(start, end)] of the gutters across r: horizontal ones (`across`), else vertical."""
        x1, y1, x2, y2 = r
        size = (1, y2 - y1) if across else (x2 - x1, 1)
        lo = y1 if across else x1
        share = {k: m.crop(r).resize(size, Image.Resampling.BOX).tobytes() for k, m in marks.items()}
        kinds = ["w" if w <= NOISE * 255 else "b" if b <= NOISE * 255 else ""
                 for w, b in zip(share["w"], share["b"])] + [""]
        out, start = [], 0
        for i in range(1, len(kinds)):
            if kinds[i] == kinds[start]:
                continue
            if kinds[start] == "w" or (kinds[start] == "b" and i - start >= run_b):
                if out and out[-1][1] == lo + start:  # white beside black: one gutter
                    out[-1] = (out[-1][0], lo + i)
                else:
                    out.append((lo + start, lo + i))
            start = i
        return out

    def trim(r):
        """r without the gutters (or the page's margin) at its edges, or None if nothing is left."""
        for across in (True, False):
            lo, hi = (r[1], r[3]) if across else (r[0], r[2])
            a, b = lo, hi
            for start, end in gutters(r, across):
                a = end if start == lo else a
                b = start if end == hi else b
            if a >= b:
                return None
            r = (r[0], a, r[2], b) if across else (a, r[1], b, r[3])
        return r

    found = []

    def cut(r, depth=0):
        r = trim(r)
        if r is None or (r[2] - r[0]) * (r[3] - r[1]) < MIN_PANEL * W * H:
            return
        for across in (True, False) if depth < 10 else ():
            lo, hi = (r[1], r[3]) if across else (r[0], r[2])
            inner = [(s, e) for s, e in gutters(r, across) if s > lo and e < hi and e - s >= run_w]
            if inner:
                edges = [lo] + [v for s, e in inner for v in (s, e)] + [hi]
                for a, b in zip(edges[::2], edges[1::2]):
                    cut((r[0], a, r[2], b) if across else (a, r[1], b, r[3]), depth + 1)
                return
        found.append(r)

    cut((0, 0, W, H))
    return [(round(x1 / W, 4), round(y1 / H, 4), round((x2 - x1) / W, 4), round((y2 - y1) / H, 4))
            for x1, y1, x2, y2 in found]


def prepare(image: bytes, order="rtl", max_side=MAX_SIDE):
    """(the JPEG the model gets, the panels' places in reading order). When the panels were found, they
    are outlined and numbered on the JPEG; else the list is empty and the JPEG is the page as it is."""
    from PIL import Image
    with Image.open(io.BytesIO(image)) as im:
        im = im.convert("RGB")
    scale = min(1.0, max_side / max(im.size))
    if scale < 1:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    try:
        frames = [p["box"] for p in order_panels([{"box": list(b)} for b in find_frames(im)], order)]
    except Exception:  # a page this can't read is still described, by the model alone
        frames = []
    if not 2 <= len(frames) <= MAX_FRAMES:
        frames = []
    out = io.BytesIO()
    (numbered(im, frames) if frames else im).save(out, "JPEG", quality=88)
    return out.getvalue(), frames


def numbered(im, frames):
    """The page with each panel outlined in red and its number in a red square in its top left corner."""
    from PIL import ImageDraw, ImageFont
    im = im.copy()
    d = ImageDraw.Draw(im)
    W, H = im.size
    line = max(3, round(max(W, H) / 250))
    size = max(16, round(max(W, H) / 32))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow before 10.1
        font = ImageFont.load_default()
    for n, (x, y, w, h) in enumerate(frames, 1):
        box = [x * W, y * H, (x + w) * W - 1, (y + h) * H - 1]
        d.rectangle(box, outline=(230, 0, 0), width=line)
        tag = [box[0], box[1], box[0] + size * (1.1 if n < 10 else 1.7), box[1] + size * 1.3]
        d.rectangle(tag, fill=(230, 0, 0))
        d.text((tag[0] + size * 0.25, tag[1] + size * 0.1), str(n), fill="white", font=font)
    return im


def panels_for(panels, frames, order="rtl") -> list:
    """The model's panels, in reading order. With the panels found in the picture (`frames`), each
    description goes to the panel whose number it gives (else to the next one in turn), and two
    descriptions of the same panel become one."""
    if not frames:
        return fix_panels(order_panels(panels, order))
    out = [{"box": list(f), "scene": "", "lines": []} for f in frames]
    for i, p in enumerate(panels):
        n = p.get("n")
        if not (isinstance(n, int) and 1 <= n <= len(frames)):
            n = min(i, len(frames) - 1) + 1
        home = out[n - 1]
        scene = p.get("scene", "")
        if scene and scene.casefold() not in home["scene"].casefold():
            home["scene"] = " ".join(x for x in (_sentence(home["scene"]), _sentence(scene)) if x)
        home["lines"] += p.get("lines") or []
    return [p for p in out if p["scene"] or p["lines"]]


# --- talking to Ollama ------------------------------------------------------------------------------------

BOX = {"type": "array", "items": {"type": "integer"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "panels": {"type": "array", "items": {"type": "object", "properties": {
            "box": BOX,
            "scene": {"type": "string"},
            "people_shown": {"type": "boolean"},
            "lines": {"type": "array", "items": {"type": "object", "properties": {
                "original": {"type": "string"},
                "kind": {"type": "string", "enum": list(KINDS)},
                "place": {"type": "string", "enum": list(PLACES)},
                "speaker": {"type": "string"},
                "text": {"type": "string"}},
                "required": ["original", "kind", "place", "speaker", "text"]}}},
            "required": ["box", "scene", "people_shown", "lines"]}},
        "summary": {"type": "string"},
        "characters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "look": {"type": "string"}},
            "required": ["name", "look"]}},
    },
    "required": ["panels", "summary", "characters"],
}

NUMBERED = json.loads(json.dumps(SCHEMA))  # a page whose panels were found: their number, not their place
_panel = NUMBERED["properties"]["panels"]["items"]
_panel["properties"] = {"panel": {"type": "integer"},
                        **{k: v for k, v in _panel["properties"].items() if k != "box"}}
_panel["required"] = ["panel"] + [k for k in _panel["required"] if k != "box"]

_NO_THINK = set()  # Ollama servers that refused "think" (older versions): asked once, then left out


def ask(ctx, prompt: str, picture: bytes, temperature=0.2, numbered=False) -> dict:
    """One page to Ollama's own API, with the answer held to SCHEMA (NUMBERED for a page whose panels
    were found). Returns the parsed answer."""
    base = _url(ctx.setting("ollama_url"))
    body = {
        "model": ctx.setting("model") or "gemma4",
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(picture).decode()]}],
        "stream": False,
        "format": NUMBERED if numbered else SCHEMA,
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


def describe_prompt(language, info, characters, before, order="rtl", frames=0) -> str:
    """The question for one page. `frames`: how many panels were found and numbered on the picture
    (0: none, the model finds them itself)."""
    known = "\n".join(f"- {c['name']}: {c['look']}" for c in characters) or "(none yet)"
    parts = [
        "You describe manga pages for a reader who is blind, so they can follow the story by listening.",
        "",
        "Reading order: " + ORDERS.get(order, ORDERS["rtl"]),
        "",
    ]
    if frames:
        parts += [
            f"The page has {frames} panels. Each one is outlined in red on the picture, with its number "
            f"(1 to {frames}, already in reading order) in a red square in its top left corner. The red "
            "outlines and numbers are not part of the manga: don't describe them.",
            "Everything drawn inside a red outline belongs to that panel: speech bubbles, text without a "
            "bubble, narration, sound effects, and small faces or figures in a corner. A bubble that "
            "crosses an outline belongs to the panel where most of it is; text outside every outline, to "
            "the nearest panel.",
            "",
            f"For each of the {frames} panels, in order, one entry:",
            "- panel: its number.",
        ]
    else:
        parts += [
            "What a panel is: a part of the page inside its own frame (a drawn border), separated from the "
            "other panels by white or black gutters. First find the frames; there are usually 3 to 7 on a page. "
            "Everything drawn inside a frame belongs to that panel: speech bubbles, text without a bubble, "
            "narration, sound effects, and small faces or figures in a corner next to a bubble. Text without a "
            "bubble, in a corner or at the edge, is never a panel of its own: it is part of the panel it is in. "
            "A panel always shows a picture, not only text.",
            "",
            "For every panel, in reading order:",
            "- box: where the panel's frame is: [x1, y1, x2, y2], on a 0-1000 scale of the page's width and "
            "height. The whole frame, not only the part with the text.",
        ]
    parts += [
        "- scene: one or two plain sentences on what the panel shows: who is there, what they do, their "
        "faces and the place. Say what matters for the story; don't repeat the dialogue.",
        "- people_shown: true if at least one character (a person, or a small face) is drawn in this "
        "panel; false if it shows only bubbles, a place or a thing.",
        "- lines: every piece of text in the panel, in reading order:",
        "  - original: the text exactly as written, in its own language and script.",
        "  - kind: speech (a speech bubble), thought (a thought bubble, often cloud-shaped), narration "
        "(a box with the narrator's text), sign (text that is part of the picture, like a sign or a "
        "letter), sound (a sound effect drawn into the art), or free (text written straight on the art "
        "with no outline around it).",
        "    A bubble is any text with an outline around it, smooth, rough, jagged or wavy, usually with a "
        "tail: that is speech (or thought). Only text with no outline at all is free. Such text is almost "
        "never a character talking: it's a sound, background noise or voices, a narrator, or someone's "
        "inner thoughts. Never make it speech: use sound for a noise, narration for a narrator's words, "
        "free for anything else (inner thoughts, murmurs, words floating beside someone), and don't say "
        "who it is from.",
        "  - place: where the bubble or text is in the panel: right, left, top, bottom or middle.",
        "  - speaker: who says or thinks it, ONLY when that character is drawn in this same panel (the "
        "bubble points to them): their name if you know it, otherwise a short description (\"the boy on "
        "the left\"). Never guess the speaker from another panel: when the one speaking isn't drawn in "
        "this panel, leave it empty. Empty for narration, signs, sounds and free text.",
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


INSIDE = 0.6       # a "panel" with at least this much of its area inside a bigger one is part of it
SMALL = 0.04       # a "panel" smaller than this part of the page, on its own, is text or a face, not a panel


def fix_panels(panels) -> list:
    """Puts back together what a small model split off a panel: text without a bubble in a corner, a
    sound effect, or a small face beside a bubble, given as a panel of its own. Such a "panel" lies
    mostly inside a real one, or is tiny, or has no picture of its own (only text). Its text goes to
    the panel it belongs to (inside, or else the nearest), and what it shows is added to that panel's
    scene. The panels must already be in reading order; panels without a place are left as they are."""
    def area(b):
        return b[2] * b[3]

    def inside(a, b):
        """How much of a lies inside b, 0 to 1."""
        w = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
        h = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        return w * h / area(a) if area(a) else 0.0

    def gap(a, b):
        dx = max(0.0, max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2]))
        dy = max(0.0, max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]))
        return dx * dx + dy * dy

    panels = [dict(p, lines=list(p.get("lines") or [])) for p in panels]
    while len(panels) > 1:
        placed = [p for p in panels if p.get("box")]
        merge = None
        for p in sorted(placed, key=lambda p: area(p["box"])):
            others = [q for q in placed if q is not p]
            if not others:
                break
            home = max(others, key=lambda q: inside(p["box"], q["box"]))
            if inside(p["box"], home["box"]) >= INSIDE and area(home["box"]) > area(p["box"]):
                merge = (p, home)
            elif area(p["box"]) < SMALL or not p.get("scene"):
                merge = (p, min(others, key=lambda q: (gap(p["box"], q["box"]), -inside(p["box"], q["box"]),
                                                       -area(q["box"]))))
            if merge:
                break
        if not merge:
            break
        part, home = merge
        before = panels.index(part) < panels.index(home)
        home["lines"] = part["lines"] + home["lines"] if before else home["lines"] + part["lines"]
        if part.get("scene") and part["scene"].casefold() not in home.get("scene", "").casefold():
            home["scene"] = " ".join(x for x in (_sentence(home.get("scene")), _sentence(part["scene"])) if x)
        panels.remove(part)
    return panels


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
    "speaker", "place", "text", "original"}]}], "summary", "characters"}. A panel's box is (x, y, w, h)
    fractions, used only to put the panels in reading order. In a panel with nobody drawn in it, nobody
    is named as the speaker: the model would guess from the panels around it."""
    if not isinstance(answer, dict):
        raise RuntimeError("The model's answer wasn't a description")
    panels = []
    for p in answer.get("panels") or []:
        if not isinstance(p, dict):
            continue
        lines = []
        nobody = p.get("people_shown") is False
        for line in p.get("lines") or []:
            if not isinstance(line, dict):
                continue
            text = _text(line.get("text"))
            if not text:
                continue
            kind = line.get("kind") if line.get("kind") in KINDS else "speech"
            speaker = _text(line.get("speaker"), 80) if kind in SAYS and not nobody else ""
            lines.append({"kind": kind, "speaker": speaker, "text": text, "original": _text(line.get("original")),
                          "place": line.get("place") if line.get("place") in PLACES else ""})
        scene = _text(p.get("scene"))
        if scene or lines:
            panel = {"box": _box(p.get("box")), "scene": scene, "lines": lines}
            if isinstance(p.get("panel"), int) and not isinstance(p.get("panel"), bool):
                panel["n"] = p["panel"]  # the number of a panel found in the picture (see panels_for)
            panels.append(panel)
    return {"panels": panels, "summary": _text(answer.get("summary"), 300),
            "characters": answer.get("characters") or []}


SAYS = {"speech": "says", "thought": "thinks"}


def speech_for(data, page, detail="full") -> str:
    """The page as text to listen to, in reading order. `detail`: "full" (each panel, then its text),
    "text" (only the text: for a reader who sees the pictures but can't read the text) or "scene"
    (only what each panel shows: for a reader who reads the text but can't make out the pictures).
    "Page 5." first, so a listener knows where they are.

    Page 5.
    Panel 1. Two boys stand in the rain.
    The boy on the right says: Hey, are you listening?
    It's raining again.                 (the same speaker, the next bubble: not named again)
    Sound: BOOM (an explosion).
    """
    out = [f"Page {page + 1}."]
    panels = data.get("panels") or []
    scenes = detail != "text"   # what each panel shows
    texts = detail != "scene"   # who says what, signs and sounds
    last = None  # who spoke the line just before: the same speaker again isn't named again
    for n, p in enumerate(panels, 1):
        if scenes:
            head = f"Panel {n}." if len(panels) > 1 else ""
            out.append(" ".join(x for x in (head, _sentence(p.get("scene"))) if x))
            last = None  # after a description, say who speaks again
        for line in _joined(p.get("lines") or []) if texts else ():
            who = (line.get("kind"), line.get("speaker", "").casefold()) if line.get("speaker") else None
            out.append(_sentence(line.get("text")) if who and who == last else _say_line(line))
            last = who
    if len(out) == 1:
        out.append("No text on this page." if not scenes else "Nothing to describe on this page.")
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
    if kind in SAYS and who:
        return f"{who[:1].upper() + who[1:]} {SAYS[kind]}: {_sentence(text)}"
    if kind in SAYS:  # who says it isn't drawn here: where the bubble is, not a guess
        bubble = "Speech bubble" if kind == "speech" else "Thought bubble"
        place = line.get("place")
        where = " in the middle" if place == "middle" else f" on the {place}" if place else ""
        return f"{bubble}{where}: {_sentence(text)}"
    label = {"narration": "Narration", "sign": "Written", "sound": "Sound",
             "free": "Text without a bubble"}.get(kind, "Text")
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
