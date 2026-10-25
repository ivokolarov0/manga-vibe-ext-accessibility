"""Tests with a fake Ollama. Run: python -m pytest (with MangaVibe's requirements installed)."""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import TMP

PID = "manga-accessibility"
PLUGINS_DIR = TMP / "data" / "plugins"

# what a model answers for a page: two panels, right to left
ANSWER = {
    "panels": [
        {"box": [500, 0, 1000, 500], "scene": "Haruka and a boy stand in the rain",
         "lines": [{"original": "ねえ、聞いてる？", "kind": "speech", "speaker": "Haruka",
                    "text": "Hey, are you listening?", "box": [700, 50, 900, 300]},
                   {"original": "ザーッ", "kind": "sound", "speaker": "the rain",
                    "text": "ZAAA (heavy rain)", "box": [0, 0, 0, 0]}]},
        {"box": [0, 0, 500, 500], "scene": "The boy looks away.",
         "lines": [{"original": "…", "kind": "thought", "speaker": "the boy", "text": "Not again", "box": None}]},
    ],
    "summary": "Haruka talks to a boy in the rain.",
    "characters": [{"name": "Haruka", "look": "girl with short black hair"},
                   {"name": "the boy", "look": "boy in a cap"}],
}


class FakeOllama:
    def __init__(self, answer=ANSWER, models=("gemma4:latest",), vision=True):
        self.answer, self.models, self.vision, self.asked = answer, models, vision, []

    def __call__(self, request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"] + (["vision"] if self.vision else [])})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        self.asked.append(body)
        assert body["messages"][0]["images"] and body["format"]["required"]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(self.answer)}})


@pytest.fixture()
def plugin(client, monkeypatch):
    from app.plugins import manager
    dest = PLUGINS_DIR / "manga_accessibility.py"
    shutil.copy(Path(__file__).with_name("manga_accessibility.py"), dest)
    client.post("/api/plugins/reload")
    p = manager.get(PID)
    module = sys.modules[type(p).__module__]
    fake = FakeOllama()
    monkeypatch.setattr(module, "_http", lambda timeout: httpx.Client(transport=httpx.MockTransport(fake)))
    client.put(f"/api/plugins/{PID}/settings", json={"ollama_url": "pc:11434", "detail": ""})
    forget(p)
    yield p, module, fake
    p.run_global_action(None, "stop")
    forget(p)
    dest.unlink()
    client.post("/api/plugins/reload")


def forget(p):
    """Every saved description and character list: each test starts from nothing."""
    from app.db import SessionLocal
    from app.models import Series
    from app.plugins import Context
    with SessionLocal() as db:
        ctx = Context(p, db)
        ctx.clear_page_data()
        for (sid,) in db.query(Series.id):
            ctx.clear_series_data(sid)
        db.commit()


def first_book(client, folder="Series B"):
    """By folder name (other tests change titles), like MangaVibe's own overlay tests."""
    from app.db import SessionLocal
    from app.models import Series
    with SessionLocal() as db:
        sid = db.query(Series.id).filter_by(folder_name=folder).scalar()
    return client.get(f"/api/series/{sid}/books").json()[0]


def wait_ready(client, book_id, page, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        layer = client.get(f"/api/books/{book_id}/overlays/{page}").json()["layers"][0]
        if not layer["pending"]:
            return layer
        time.sleep(0.05)
    raise AssertionError("page never got ready")


def test_describes_a_page(client, plugin):
    p, module, fake = plugin
    book = first_book(client)
    layer = wait_ready(client, book["id"], 0)
    assert layer["message"] == ""
    assert layer["boxes"] == []  # nothing is drawn over the page
    if module.HAS_SPEECH:
        assert layer["speech"] == (
            "Page 1.\n"
            "Panel 1. Haruka and a boy stand in the rain.\n"
            "Haruka says: Hey, are you listening?\n"
            "Sound: ZAAA (heavy rain).\n"
            "Panel 2. The boy looks away.\n"
            "The boy thinks: Not again.")
    if module.HAS_NOTE:
        assert layer["note"] == "Haruka talks to a boy in the rain."
    # the page went to Ollama as a JPEG, with the language in the question
    image = fake.asked[0]["messages"][0]["images"][0]
    assert image.startswith("/9j/")  # base64 of a JPEG's first bytes
    assert "in English" in fake.asked[0]["messages"][0]["content"]
    # the model isn't asked where the bubbles are any more
    line_schema = fake.asked[0]["format"]["properties"]["panels"]["items"]["properties"]["lines"]["items"]
    assert "box" not in line_schema["properties"]


def test_characters_are_kept_per_series(client, plugin):
    p, module, fake = plugin
    if not hasattr(module, "HAS_NOTE") or not module.HAS_NOTE:
        pytest.skip("needs MangaVibe with plugin API 4")
    book = first_book(client)
    wait_ready(client, book["id"], 0)
    wait_ready(client, book["id"], 1)
    from app.db import SessionLocal
    from app.plugins import Context
    with SessionLocal() as db:
        chars = Context(p, db).get_series_data(book["series_id"], "characters")
    assert chars == [{"name": "Haruka", "look": "girl with short black hair"}]  # "the boy" is not a name
    # a later page is asked with the list, and with the page before
    later = [a["messages"][0]["content"] for a in fake.asked[1:]]
    assert any("- Haruka: girl with short black hair" in q for q in later)
    assert any("The page before: Haruka talks to a boy in the rain." in q for q in later)


def test_text_only(client, plugin):
    p, module, fake = plugin
    client.put(f"/api/plugins/{PID}/settings", json={"detail": "text"})
    book = first_book(client)
    layer = wait_ready(client, book["id"], 0)
    if module.HAS_SPEECH:
        assert layer["speech"] == ("Page 1.\nHaruka says: Hey, are you listening?\nSound: ZAAA (heavy rain).\n"
                                   "The boy thinks: Not again.")


def test_badges_and_book_actions(client, plugin):
    p, module, fake = plugin
    book = first_book(client)
    r = client.post(f"/api/books/{book['id']}/actions/{PID}/chapter").json()
    end = time.time() + 15
    while time.time() < end and "Described" != (first_book(client)["badges"] or [{}])[0].get("text"):
        time.sleep(0.05)
    assert first_book(client)["badges"][0]["text"] == "Described", r
    assert len(fake.asked) == book["page_count"]
    # again: every page is asked again (none skipped because the worker still saw the old ones)
    client.post(f"/api/books/{book['id']}/actions/{PID}/again")
    end = time.time() + 15
    while time.time() < end and len(fake.asked) < 2 * book["page_count"]:
        time.sleep(0.05)
    time.sleep(0.3)
    assert len(fake.asked) == 2 * book["page_count"]
    assert first_book(client)["badges"][0]["text"] == "Described"


def test_a_bad_answer_is_saved_as_failed(client, plugin):
    p, module, fake = plugin
    fake.answer = "not a description"
    book = first_book(client)
    layer = wait_ready(client, book["id"], 0)
    assert layer["message"].startswith("Couldn't describe this page")
    if module.HAS_SPEECH:
        assert layer["speech"] == "Page 1. This page couldn't be described."


def test_without_a_server(client, plugin):
    client.put(f"/api/plugins/{PID}/settings", json={"ollama_url": ""})
    layer = client.get(f"/api/books/{first_book(client)['id']}/overlays/0").json()["layers"][0]
    assert not layer["pending"] and "set the Ollama server" in layer["message"]


def test_test_connection(client, plugin):
    p, module, fake = plugin
    assert client.post(f"/api/plugins/{PID}/actions/test").json()["message"] == "Connected: gemma4 is ready"
    fake.models = ("llama3:latest",)
    assert "ollama pull gemma4" in client.post(f"/api/plugins/{PID}/actions/test").json()["message"]
    fake.models, fake.vision = ("gemma4:latest",), False
    assert "can't see pictures" in client.post(f"/api/plugins/{PID}/actions/test").json()["message"]


def test_clean_and_speech_from_odd_answers():
    """The model's numbers and words, cleaned up without MangaVibe."""
    import importlib
    module = importlib.import_module("manga_accessibility")
    assert module._box([100, 200, 300, 400]) == [0.1, 0.2, 0.2, 0.2]
    assert module._box([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.2, 0.2]      # fractions
    assert module._box([300, 400, 100, 200]) == [0.1, 0.2, 0.2, 0.2]      # corners swapped
    assert module._box([0, 0, 0, 0]) is None and module._box("x") is None and module._box([1, 2]) is None
    data = module.clean({"panels": [{"scene": "", "lines": [{"text": " Hi ", "kind": "shout", "speaker": ""}]}],
                         "summary": "s"})
    assert data["panels"][0]["lines"][0]["kind"] == "speech"
    assert module.speech_for(data, 4) == "Page 5.\nSomeone says: Hi."
    assert module.speech_for({"panels": []}, 0) == "Page 1.\nNothing to describe on this page."
    assert module._url("pc:11434/v1/") == "http://pc:11434"


def test_panels_in_reading_order():
    import importlib
    ma = importlib.import_module("manga_accessibility")
    p = lambda name, x, y, w, h: {"name": name, "box": [x, y, w, h]}  # noqa: E731
    # a row of two, then a tall panel on the right beside two small ones on the left
    page = [p("top-left", 0, 0, 0.5, 0.3), p("top-right", 0.5, 0, 0.5, 0.3),
            p("low-left-1", 0, 0.32, 0.5, 0.3), p("low-left-2", 0.01, 0.65, 0.48, 0.3),
            p("tall-right", 0.5, 0.32, 0.5, 0.65)]
    names = lambda panels: [x["name"] for x in panels]  # noqa: E731
    assert names(ma.order_panels(page, "rtl")) == ["top-right", "top-left", "tall-right", "low-left-1", "low-left-2"]
    assert names(ma.order_panels(page, "ltr")) == ["top-left", "top-right", "low-left-1", "low-left-2", "tall-right"]
    assert names(ma.order_panels(page, "vertical"))[:2] == ["top-left", "top-right"]
    # a panel without a place: the model's order is kept
    assert ma.order_panels(page[:1] + [{"name": "x", "box": None}], "rtl")[1]["name"] == "x"


def test_the_same_speaker_is_named_once():
    import importlib
    ma = importlib.import_module("manga_accessibility")
    line = lambda who, text, kind="speech": {"kind": kind, "speaker": who, "text": text}  # noqa: E731
    data = {"panels": [{"scene": "Haruka waves.", "lines": [line("Haruka", "Hi"), line("Haruka", "Wait"),
                                                            line("Taro", "What?"), line("Haruka", "Nothing")]},
                       {"scene": "Haruka runs.", "lines": [line("Haruka", "Bye"), line("Haruka", "Hm", "thought")]}]}
    assert ma.speech_for(data, 0) == ("Page 1.\nPanel 1. Haruka waves.\nHaruka says: Hi.\nWait.\nTaro says: What?\n"
                                      "Haruka says: Nothing.\nPanel 2. Haruka runs.\nHaruka says: Bye.\nHaruka thinks: Hm.")
    # only the text: no description in between, so the next panel goes on without the name
    assert ma.speech_for(data, 0, "text").split("\n")[4:6] == ["Haruka says: Nothing.", "Bye."]
    # people without a name are never joined: two of them may be two people
    two = {"panels": [{"scene": "", "lines": [line("", "A"), line("", "B")]}]}
    assert ma.speech_for(two, 0, "text") == "Page 1.\nSomeone says: A.\nSomeone says: B."


def test_names_and_descriptions():
    import importlib
    ma = importlib.import_module("manga_accessibility")
    assert all(ma.is_name(n) for n in ("Haruka", "Bam-chan", "Okarun", "Mr. Turbo Granny"))
    assert not any(ma.is_name(n) for n in ("person on the left", "the boy", "Tall man", "unknown", "", "x" * 41))


def test_reading_order_from_the_series(client, plugin):
    """The panels are read the way the series reads in MangaVibe."""
    p, module, fake = plugin
    book = first_book(client)
    client.patch(f"/api/series/{book['series_id']}", json={"reader_mode": "ltr"})
    try:
        wait_ready(client, book["id"], 0)
        assert "LEFT TO RIGHT" in fake.asked[0]["messages"][0]["content"]
        client.patch(f"/api/series/{book['series_id']}", json={"reader_mode": "rtl"})
        client.post(f"/api/books/{book['id']}/actions/{PID}/again")
        wait_ready(client, book["id"], 0)
        assert "RIGHT TO LEFT" in fake.asked[-1]["messages"][0]["content"]
    finally:
        client.patch(f"/api/series/{book['series_id']}", json={"reader_mode": "default"})


def test_an_answer_that_never_ends_fails_the_page(client, plugin):
    p, module, fake = plugin

    def looping(request):  # a model repeating itself until the length limit: the JSON never closes
        fake(request)      # (records what was asked)
        return httpx.Response(200, json={"message": {"content": '{"panels": [{"scene": "a a a a'},
                                         "done_reason": "length"})
    module._http = lambda timeout: httpx.Client(transport=httpx.MockTransport(looping))
    book = first_book(client)
    layer = wait_ready(client, book["id"], 0)
    assert "went on too long" in layer["message"]
    temps = [a["options"]["temperature"] for a in fake.asked]
    assert temps[:2] == [0.2, 0.6] and fake.asked[0]["options"]["num_predict"] == module.MAX_TOKENS


def test_a_timeout_fails_the_page_instead_of_waiting_forever(client, plugin):
    """Ollama busy for too long (e.g. with another program) is a failed try, not "offline" forever."""
    p, module, fake = plugin

    def slow(request):
        raise httpx.ReadTimeout("no answer", request=request)
    module._http = lambda timeout: httpx.Client(transport=httpx.MockTransport(slow))
    book = first_book(client)
    layer = wait_ready(client, book["id"], 0)
    assert "didn't answer" in layer["message"]
    assert p.offline == ""


def test_signs_and_sounds_next_to_each_other_are_one_line():
    import importlib
    ma = importlib.import_module("manga_accessibility")
    sign = lambda t: {"kind": "sign", "speaker": "", "text": t}  # noqa: E731
    data = {"panels": [{"scene": "", "lines": [sign("Oden"), sign("Beef."), sign("oden"), sign("Udon"),
                                               {"kind": "sound", "speaker": "", "text": "BAM"},
                                               {"kind": "sound", "speaker": "", "text": "BAM"}]}]}
    assert ma.speech_for(data, 0, "text") == "Page 1.\nWritten: Oden, Beef, Udon.\nSound: BAM."
