# Manga Accessibility for MangaVibe

Manga for readers who are **blind or have low vision**. While you read in
[MangaVibe](https://github.com/ivokolarov0/manga-vibe), each page is described in reading order:
what every panel shows, who says what, and what the sound effects mean. MangaVibe reads it aloud,
or your screen reader does.

```
Page 1.
Panel 1. Two boys stand in the rain, looking at each other.
The boy on the right says: Hey, are you listening?
The boy on the left says: It's raining today too.
Panel 2. A close-up of the boy on the right. He looks annoyed.
The boy on the right says: Did you bring an umbrella?
Sound: ZAAA (heavy rain).
```

Nothing is drawn over the page: the plugin only looks at the picture and its text and tells you what
happens. The text is translated into your language if the manga is in another one.

**Free and private.** Everything runs on your own computer with [Ollama](https://ollama.com). No
account, no subscription, and your manga never leaves your home network.

**Simple setup:** one program (Ollama) and one model. Each page is described once and saved.

## What you need

- **MangaVibe**, the latest version
- **A computer with a graphics card** on the same home network (or the same computer that runs
  MangaVibe), switched on while you read new chapters:
  - 8 GB of graphics memory or more for `gemma4` (Mac: Apple Silicon with 16 GB of memory)
  - 12 GB or more for `gemma4:12b`, which gets the order of the panels right more often

## Setup (about 10 minutes)

### 1. Install Ollama

- **Windows / Mac:** go to [ollama.com](https://ollama.com), click **Download** and install it.
- **Linux:** run `curl -fsSL https://ollama.com/install.sh | sh`

### 2. Download the model

Open a command window (Windows: press the **Windows key**, type `cmd`, press **Enter**) and run:

```
ollama pull gemma4
```

It downloads about 7 GB. Wait until it says **success**. (With a big graphics card, use
`ollama pull gemma4:12b` instead.)

### 3. Let MangaVibe talk to Ollama

Ollama only answers programs on its own computer until you change one setting. Skip this step if
MangaVibe runs on the same computer **without** Docker.

- **Windows:** press the **Windows key**, type `environment`, click **Edit environment variables for
  your account** → **New…** → name `OLLAMA_HOST`, value `0.0.0.0` → **OK** → **OK**. Then right-click the
  llama icon near the clock → **Quit Ollama**, and start Ollama again from the Start menu.
- **Mac:** in Terminal run `launchctl setenv OLLAMA_HOST "0.0.0.0"`, then quit Ollama from the menu bar
  and start it again. (Do this again after restarting the Mac.)
- **Linux:** run `sudo systemctl edit ollama`, add these two lines, save, then run
  `sudo systemctl restart ollama`:
  ```
  [Service]
  Environment="OLLAMA_HOST=0.0.0.0"
  ```

You'll need the computer's address on your network. On Windows, run `ipconfig` and look for
**IPv4 Address** (e.g. `192.168.1.50`).

### 4. Install the plugin in MangaVibe

1. Download [`manga_accessibility.py`](manga_accessibility.py) (click it, then the download button).
2. Copy it into the `plugins` folder in MangaVibe's data folder (`docker/mangavibe/plugins` on a NAS).
3. In MangaVibe: **Settings → Plugins → Reload plugins**.
4. Open **Manga Accessibility → Settings** and fill in:
   - **Ollama server:** `http://192.168.1.50:11434` (your computer's address, port `11434`)
   - **Model:** `gemma4` (or `gemma4:12b`)
5. Press **Test connection**. It should say **Connected: gemma4 is ready**.

## Reading

1. Open a chapter. Tap the middle of the page, then the **☰** button (Reading options).
2. Under **Plugins** (at the bottom), tap **Manga Accessibility**. Everything for this plugin is in the
   panel that opens.
3. Switch **Describe pages** on. The first page takes a few seconds; the next pages are described ahead
   while you read.
4. Switch **Read aloud** on. Under it:
   - **Reading speed:** 0.75× to 2×
   - **Turn pages by itself:** after a page is read, the next one comes, and after the last page the next
     chapter. You can listen to a whole chapter without touching the screen.
   - **Voice** (in a browser): pick one of your device's voices. In the Android app, the phone's voice is
     used (Android settings → Text-to-speech).

**With a screen reader** (TalkBack, NVDA, VoiceOver), leave Read aloud off: your screen reader reads
each page's description by itself, in your own voice and speed.

**For low vision**, **Page look** in the reading options has **Brightness**, **Contrast** and **Page colours**
(Inverted turns white pages black; Warm takes out the blue light). They're in Settings → Reader too.

### Settings

| Setting | |
|---|---|
| Language | The descriptions are written in this language, and the text is translated into it. Default: English |
| What is read out | **Everything** (each panel described, then who says what) or **Only the text** (who says what) |
| Pages to describe ahead | How many pages after yours are prepared in advance. Default: 4 |

### Buttons

- In the reading options, under **Plugins → Manga Accessibility**: **Describe this chapter**, **Describe this chapter again**
- On the series page: **Describe the whole series**, **Forget this series' descriptions**
- In Settings → Plugins: **Test connection**, **Stop the queue**

The series list shows how far each chapter is, e.g. **Described 12/40**.

## Good to know

- **The model can make mistakes**: sometimes it puts two panels in the wrong order, misreads a word or
  sees something that isn't there. `gemma4:12b` makes fewer mistakes than `gemma4`, but it's slower.
- **Characters keep their names:** when a name is said on a page, the plugin remembers what that
  character looks like, so they're called by their name on the following pages. When one character
  says several bubbles in a row, the name is said once.
- **No guessing who speaks:** a speaker is named only when they're drawn in the same panel. In a panel
  with only bubbles (nobody drawn in it), you hear where the bubble is instead: "Speech bubble on the
  right: …".
- **The panels are found in the picture:** before a page goes to the model, the plugin finds its panels
  from the white (or black) lines between them and numbers them, so text in a corner or a small face
  next to a bubble stays part of its panel. On a page with slanted panels or one picture across the
  whole page, the model finds the panels itself.
- **Panels in the right order:** pages are read the way the series reads in MangaVibe. A series matched
  on AniList or MangaUpdates as a manga from Japan reads right to left (also when it's in English); a
  manhwa, manhua or western comic left to right. A series that isn't matched follows its library
  folder's direction (Settings → Library folders), which is manga unless you change it. You can also set
  it on the series page (⋯ → Reading direction). The plugin tells the model the direction and then puts
  the panels in that order by where they are on the page, since a small model doesn't always keep to it.
- **Sharing Ollama with Manga Translate:** Ollama does one thing at a time, so while the translator is
  busy, pages wait their turn. The status line in Settings → Plugins shows the page being described and
  for how long. A page that gets no answer in 10 minutes, or whose answer never ends (a model repeating
  itself), is tried once more and then skipped.
- A page takes about 5–15 seconds, depending on the model and the graphics card.

## Try it without MangaVibe

`try_pages.py` describes a few page images and prints what would be read out:

```
uv run try_pages.py page1.jpg page2.jpg --ollama http://localhost:11434 --model gemma4
```

## Tests

```
python -m pytest
```

They use MangaVibe's own test setup and a fake Ollama. MangaVibe is expected next to this folder
(`../manga-vibe`); set `MANGAVIBE_DIR` to use another checkout.

## Licence

MIT, see [LICENSE](LICENSE). Use it with manga you own.
