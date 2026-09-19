# Watching, listening, and returning to Japanese

Start the usual server with `uv run uvicorn server:app --host 0.0.0.0 --port 8000`,
then open `/library`. Add your media to populate the library; the repository
does not include shows, audio, subtitles, or a prepared catalogue. Source IDs
are assigned locally, so open items through the library.

## Today's loop

Watch a continuous 10–20 minute chunk for the story. Tap a subtitle word for the
dictionary; Save word captures its sentence and timestamp. Save line (keyboard
`C`) or Save grammar captures the current subtitle. Select text for a shorter
phrase, or extend the span with the next-line selector. Capturing creates an
inbox item, not an Anki card or a review obligation. Capture freely when useful;
there is no one-expression limit.

Choose a short passage to return to. `R` replays the selected line/span and stops
at its end; Loop span repeats it. Hide Japanese for listening first, then reveal
it. Say the passage along with the audio and mark “I shadowed this span” after
doing it. Audio-only mode keeps the same context and player controls.

Tomorrow, the library offers captured expressions again. Listen, reveal, try
the line, and mark the revisit. Invitations start after one day; subsequent
intervals are 3, 7, then 14 days. Overdue items remain available without a penalty.
This is a repeat schedule, not a memory grade or evidence of JLPT mastery.
Previously used sources also become candidates for replay after a day.

## Explain a subtitle span

“Explain this span” uses the current line and the next-line selector (or selected
subtitle lines). It displays a translation, meaningful parts with hiragana
readings, and a short structure explanation. Text selection chooses its containing
subtitle span; the explanation keeps that full context.

The first provider is Codex `gpt-6-astra` with low reasoning. A disposable tmux
pane runs `codex exec` with a JSON output schema and writes `astra.json`.
The pane closes when finished; the user's panes are preserved. The server must
have access to the tmux session and an authenticated Codex installation. It uses
`WAKATTA_TMUX_PANE`, then inherited `TMUX_PANE`, or tmux's default target.

If tmux/Codex fails, exceeds its deadline, or returns invalid output, the job
falls back to `gemma4:12b` via local Ollama with thinking off. Both providers
use the same output shape and strict validation, including hiragana readings.
The local generation schema omits length constraints that Ollama's grammar
compiler rejected; validation still enforces those limits after generation.

Files live in `data/explanations/<text-and-prompt-version-hash>/`:
`prompt.txt`, `schema.json`, `astra.json`, and the validated `result.json` with
provider information. Identical passages reuse their saved answer. No capture
or Anki card is created. The UI identifies the provider and fallback use.
One job runs at a time; model execution is bounded, and interrupted/failed jobs
can be retried. Changing the prompt or schema requires bumping `VERSION` in
`explanations.py` to invalidate cached answers.

JSON validation checks structure, not factual accuracy. The test phrase produced
a good Astra explanation; Gemma returned valid JSON but described one modifier's
attachment imprecisely. Treat generated grammar explanations as assistance.

## What gets tracked

Playback records actual sampled playing time and saves position; seeks and
buffering do not count as time spent. Replay is recorded separately. Shadowing
and singing require an explicit click and never follow automatically from
playback. Song sources expose the singing button. Browser activity events queue
locally during connection loss and retry with stable IDs to avoid double counts.
Captures themselves require a successful server response; failures are visible.

The manga reader resumes the saved page and estimates active reading time while
visible, with recent pointer, keyboard, or scroll activity. This is an estimate,
not proof of reading or comprehension. Watching/listening time likewise measures
exposure. For evidence of improvement, revisit a familiar scene with Japanese
hidden and notice what you now understand before revealing it.

## Add and browse material

The library combines locally catalogued media with existing reading works.
Only prepared media play immediately; other episodes offer a preparation button.
Preparation copies the original over SSH from the configured media server and creates a
local browser-compatible file with Japanese audio. It needs SSH access,
`ffmpeg`, `ffprobe`, disk space, and time for conversion. Originals stay on Plex.
Known mismatched K-On episode 1 subtitles are flagged and preparation is blocked.
Other subtitles are source material, not a guarantee of perfect alignment;
use the subtitle shift control for a consistent timing offset.

Copy `.env.example` settings into your local `.env` and set `WAKATTA_PLEX_HOST`
and `WAKATTA_PLEX_ROOT` for your SSH media server. The scanner currently covers
the anime collection names listed in `media_ingest.py`; adapt that list to your
library. To refresh the catalogue:

```sh
uv run python media_ingest.py --scan
uv run python media_ingest.py --prepare 1
```

The library's YouTube form imports one episode as audio with available manually
provided Japanese captions. It uses `uv tool run --from yt-dlp yt-dlp`; network
access and site availability are required. Automatic captions are not imported.
A source without Japanese captions can play but has no interactive transcript.

Upload local video, audio, or songs with optional SRT/WebVTT timed transcripts.
For lyrics practice, select Song and supply timed lyrics. Untimed text/PDF
transcripts, automatic lyric alignment, and adding transcripts to an existing
source are not implemented yet. Fullscreen uses native Japanese captions; exit
fullscreen to tap words. Dictionary tokenization may need correction for names
or unusual phrasing.

## Storage and next iteration

`data/study.db` stores sources, cues, captures, activity, and revisit schedules.
`data/study_media/` stores prepared media. Both are ignored by Git: back them up
if the history and captures matter. Anki's collection and scheduling are not
changed by this feature. Mining-to-Anki, audio-writing generation, and deeper
recommendations are follow-up work after trying the capture/replay loop.

## Verification

```sh
uv run python -m unittest tests.test_study tests.test_explanations
node --check static/watch.js
node --check static/library.js
node --check static/study-client.js
```

`tests/browser_server.py` serves a temporary snapshot of study data for browser
checks without writing to actual study history. It needs the prepared local
media and dictionary. Start it with
`uv run uvicorn tests.browser_server:app --host 127.0.0.1 --port 8001`.
