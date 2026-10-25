# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "pillow"]
# ///
"""Try the descriptions on a few pages before using the plugin in MangaVibe.

    uv run try_pages.py PAGE [PAGE ...] [--ollama URL] [--model NAME] [--language NAME] [--text-only]
                        [--order rtl|ltr|vertical]

Sends each page to Ollama and prints what MangaVibe would read aloud. Pages are treated as one
chapter in the order given: each page gets the summary of the one before and the characters named
so far, like in MangaVibe. The description is the plugin's own code (manga_accessibility.py, next to
this file).
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import time
import types
from pathlib import Path


def load_plugin():
    """manga_accessibility.py without MangaVibe: the few MangaVibe names it imports are stand-ins."""
    class Anything:
        def __init__(self, *args, **kwargs):
            pass
    app, models, plugins = (types.ModuleType(n) for n in ("app", "app.models", "app.plugins"))
    models.Book = models.Series = object
    plugins.Plugin = object
    plugins.Action = plugins.Box = plugins.Overlay = plugins.SettingDef = Anything
    sys.modules.update({"app": app, "app.models": models, "app.plugins": plugins})
    path = Path(__file__).with_name("manga_accessibility.py")
    spec = importlib.util.spec_from_file_location("manga_accessibility", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Settings:
    """Stands in for the plugin's ctx: only ctx.setting() is used to talk to Ollama."""
    def __init__(self, values):
        self.values = values

    def setting(self, key):
        return self.values.get(key)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pages", nargs="+", type=Path)
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="gemma4")
    ap.add_argument("--language", default="English")
    ap.add_argument("--text-only", action="store_true", help="read only who says what, like the setting")
    ap.add_argument("--order", default="rtl", choices=["rtl", "ltr", "vertical"],
                    help="manga (right to left, the default), comic (left to right) or webtoon")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ma = load_plugin()
    ctx = Settings({"ollama_url": args.ollama, "model": args.model})
    characters, before = [], ""
    for n, path in enumerate(args.pages):
        start = time.monotonic()
        prompt = ma.describe_prompt(args.language, "", characters, before, args.order)
        data = ma.clean(ma.ask(ctx, prompt, ma.to_jpeg(path.read_bytes())))
        data["panels"] = ma.order_panels(data["panels"], args.order)
        for c in data.pop("characters", []):  # the same rule as the plugin: only real names
            name = str(c.get("name") or "").strip() if isinstance(c, dict) else ""
            if ma.is_name(name) and name.casefold() not in {x["name"].casefold() for x in characters}:
                characters.append({"name": name, "look": str(c.get("look") or "")})
        before = data.get("summary", "")
        print(f"--- {path.name} ({time.monotonic() - start:.1f} s) ---")
        print(ma.speech_for(data, n, "text" if args.text_only else "full"))
        print(f"Summary: {before}\n")
    if characters:
        print("Characters: " + "; ".join(f"{c['name']} ({c['look']})" for c in characters))


if __name__ == "__main__":
    main()
