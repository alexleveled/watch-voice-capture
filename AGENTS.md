# Instructions for coding agents

This repo is a self-hosted voice recorder: a Wear OS watch app uploads audio to a Windows PC, which transcribes it (WhisperX, local GPU), summarizes it with the user's chosen AI provider, and shows it in a local dashboard. [README.md](README.md) explains the product. [SETUP.md](SETUP.md) has every command.

When the user says "set this up" (or similar), run the setup below. Work through it in order, verify each step before moving on, and tell the user in one line what you are doing at each step.

## Rules

- Never print, log or commit the contents of `.env`. Refer to keys by name.
- Never run `git add .env` or anything under `data/`.
- Commands that need administrator rights (the firewall rule) or install software: show the user the exact command and get a yes first.
- Don't change how the network is exposed beyond what SETUP.md says. Default is Wi-Fi mode. Only set up tunnel mode if the user asks.
- If a step fails twice, stop and show the user the error. Don't work around it by weakening a check.
- Windows only. If the machine isn't Windows with an NVIDIA GPU, say so before installing anything.

## Setup steps

1. **Check the machine.** Run `nvidia-smi`, `py -3.12 --version`, `ffmpeg -version`, `adb version`. Offer the `winget` installs from SETUP.md step 1 for anything missing.
2. **Install the PC side.** SETUP.md step 2. Torch from the CUDA index first, then `requirements.txt`. Verify: `.venv\Scripts\python.exe -c "import torch, whisperx; print(torch.cuda.is_available())"` prints `True`.
3. **Settings file.** Copy `.env.example` to `.env` if there isn't one, then `python launch.py token`.
4. **Ask the user** (one question, not one at a time):
   - Which AI should write the summaries: Claude subscription, ChatGPT subscription, an API key, or a free local model?
   - Do they have a Hugging Face read token for speaker labels? If not, give them the link in SETUP.md step 4 and wait. They can skip it and add it later.
   Secrets go in through the dashboard once it is running (step 6), so the user never has to edit `.env` and never has to paste a key into this chat.
5. **Start it.** `python launch.py start`. Verify: `python launch.py status` shows the worker alive.
6. **Set the provider.** Either walk the user through Settings > Summaries in the dashboard, or set it over the local API:
   `PUT http://127.0.0.1:8808/api/settings` with `{"summary": {"provider": "...", "model": "...", "base_url": "...", "api_key": "..."}}`.
   Providers: `claude_subscription`, `codex_subscription`, `openai_compatible`. Then `POST /api/summary/test` and check for `"ok": true`. If it fails, the `error` field says why.
   - Claude: `claude auth status` must show logged in. If not, the user runs `claude login`.
   - Codex: `codex --version` must work. If not, `npm i -g @openai/codex`, then the user runs `codex login`.
   - Local: follow "Using a free local model" in SETUP.md. Check that Ollama (`http://localhost:11434/v1/models`) or LM Studio (`http://localhost:1234/v1/models`) answers and use a model name from that list. Suggest a small model (7B to 12B). Ask before downloading one, since it is several GB.
   - **Keys.** Point the user at the dashboard fields and let them paste: Hugging Face token under Settings > Voices > Speaker labels, API key under Settings > Summaries, calendar link under Settings > Auto mode. Each saves to `.env`. If the user would rather hand you a key, send it with the same endpoint, `{"secrets": {"hf_token": "..."}}` or `{"secrets": {"calendar_url": "..."}}`, and don't echo it back. `GET /api/settings` reports `secrets.hf_token_set` and `secrets.calendar_url_set`, never the values.
   - With a local model, ask whether they game, edit or render on this PC. If yes, suggest `"when": "daily"` with a `daily_time`, and `{"system": {"pause_when_busy": true}}` (same settings endpoint).
7. **Prove the PC side.** Upload a short speech clip: `POST /api/upload` with a `file` form field. Poll `GET /api/conversations/<recording_id>` until `status` is `done`. The first run downloads models and can take several minutes. Show the user the title and summary.
8. **Pair the watch.** This needs the user's hands. Give them SETUP.md step 5, one instruction at a time, and run the `adb pair` and `adb connect` commands for them with the numbers they read out. Verify with `adb devices`.
9. **Firewall.** SETUP.md step 6. Admin command, so ask first.
10. **Install the watch app.** If `dist/voice-capture-watch.apk` is missing, ask the user to download it from the Releases page, or build it (`cd watch; .\gradlew.bat assembleDebug`, needs Android Studio and a `watch/local.properties` with `sdk.dir`). Then `python tools/setup_watch.py`.
11. **Prove the whole path.** Ask the user to record twenty seconds on the watch. Watch `data\server.log` for a `POST /api/v1/chunks` line, then wait for the conversation to reach `done`.
12. **Finish.** Tell the user to enroll their voice (Settings > Voices), map the Home key double press to Quick Rec, and turn Wireless debugging off. Mention the battery cost of auto mode and that recording laws vary.

## Map of the code

| File | Job |
|---|---|
| `server.py` | FastAPI. Watch API under `/api/v1/*` (bearer token), dashboard API, static files, the network gate |
| `worker.py` | The pipeline loop: transcribe, label speakers, stitch, summarize, daily digest |
| `transcribe.py` | WhisperX engine, lazy model load, idle unload |
| `summarize.py` | The three summary providers and output repair |
| `stitch.py` | Joins recordings that belong to one conversation |
| `calendar_feed.py` | Optional iCal feed for the meeting trigger |
| `db.py`, `store.py` | SQLite schema, settings defaults, transcript helpers |
| `config.py` | Paths, `.env`, tunables |
| `launch.py` | start / stop / status / token |
| `tools/setup_watch.py` | Installs and configures the watch app over adb |
| `static/` | Dashboard (plain HTML, CSS, JS, no build step) |
| `watch/` | Wear OS app (Kotlin). `RecorderService.kt` is the core |

## Traps already solved (don't undo them)

- onnxruntime crashes on 32-bit ARM watches with graph optimizations on. `Vad.kt` uses `OptLevel.NO_OPT` on purpose.
- Android 14+ won't start a microphone service from the background. Auto mode keeps its service alive and uses exact alarms.
- WorkManager uploads use `ExistingWorkPolicy.REPLACE` so a fresh upload isn't stuck behind an old retry delay.
- WhisperX 3.8.6 pins torch 2.8. Install torch from the cu128 index before the requirements file.
- Don't run the server with `uvicorn --reload`.
- Local models: `summarize.py` sends `reasoning_effort: none` to local endpoints and splits conversations that exceed the model's context. If a local summary takes over a minute, check that the model's thinking is really off before changing anything else. `POST /api/summary/test` returns the seconds taken.
- If you are running inside VS Code, your shell has `ELECTRON_RUN_AS_NODE=1` set. LM Studio (and any Electron app) won't start from it, and `lms` reports "no valid installation was found". Clear the variable for that command first.
- Don't set `JAVA_HOME` for the gradle build unless it is missing. A wrong one breaks it.
