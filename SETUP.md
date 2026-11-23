# Setup by hand

Run every command from this folder in PowerShell. If you'd rather not do this yourself, open the folder in Claude Code or Codex and say "set this up for me" (see [AGENTS.md](AGENTS.md)).

## 1. Install the tools

You need Python 3.12, ffmpeg and adb on your PATH.

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
winget install Google.PlatformTools
```

Close and reopen PowerShell, then check:

```powershell
py -3.12 --version
ffmpeg -version
adb version
nvidia-smi
```

## 2. Install the PC side

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The torch line has to run first. It is a download of a few GB.

## 3. Create your settings file

```powershell
Copy-Item .env.example .env
python launch.py token
```

The second command creates the upload token the watch will use and saves it in `.env`.

You don't have to edit `.env` by hand after this. The keys you add yourself are all entered in the dashboard, which saves them to `.env` for you:

| What | Where in the dashboard |
|---|---|
| Hugging Face token (speaker labels) | Settings > Voices > Speaker labels |
| AI provider API key | Settings > Summaries > AI provider |
| Calendar link (optional) | Settings > Auto mode > When to listen |

A saved key is never shown again. The box just says "saved", and you can replace or remove it.

## 4. Start it and pick your AI

```powershell
python launch.py start
```

Open http://127.0.0.1:8808 and go to **Settings**.

**Speaker labels (recommended).** Under **Voices > Speaker labels**, follow the three steps shown there: make a free account at huggingface.co, accept the terms on https://huggingface.co/pyannote/speaker-diarization-community-1, create a read token, paste it in and press **Save token**. Without it every line is labeled as one speaker.

**Summaries.** Under **Summaries**, pick a provider and press **Save and test**.

| Provider | What to do first |
|---|---|
| Claude subscription | Install Claude Code and run `claude login` once |
| ChatGPT subscription | `npm i -g @openai/codex`, then `codex login` once |
| API key | Pick the service, paste the key, type the model name your service uses |
| Local model | See "Using a free local model" below. No key needed |

Then scroll to **Test upload**, send any audio file with speech in it, and open the conversation. The first run downloads the speech models, so give it a few minutes. When you see a transcript and a summary, the PC side is done.

Other commands: `python launch.py status`, `stop`, `restart`. Logs are in `data\server.log` and `data\worker.log`.

### Using a free local model

A summary is an easy job, so a small model is enough. A model in the 7B to 12B range (about 5 to 8 GB) fits on the GPU next to the transcription models.

**Ollama**

```powershell
winget install Ollama.Ollama
ollama pull <model>
```

Pick a small instruct model from ollama.com/library. In Settings > Summaries choose "API key or local model", service **Ollama**, and type the model name exactly as `ollama list` shows it.

**LM Studio**

```powershell
lms get qwen/qwen3.5-9b
lms server start
```

Choose service **LM Studio** and type the model name as `lms ls` shows it. In LM Studio's server settings, keep "Just-in-Time model loading" on.

**Thinking is switched off for you.** Many small models "think" for a long time before answering, which a summary doesn't need. On local endpoints the app asks the model not to (it sends `reasoning_effort: none`). On Qwen 3.5 9B that cut a short summary from 34 seconds to 4. If your model ignores that setting and summaries take a minute or more, turn reasoning off in the model's own settings, or pick a model without a thinking mode.

**Long conversations.** Local models often load with a small context (LM Studio defaulted to 8,192 tokens here, which is about 30 minutes of talk). When a conversation doesn't fit, the app summarizes it in parts and merges them, so nothing fails. For better results on long meetings, raise the model's context length to 16K or 32K in LM Studio or Ollama if your GPU has the memory.

**The model does not stay loaded.** Both tools load the model when a summary is requested and unload it after it has been idle (Ollama after 5 minutes by default, LM Studio after its idle time). To set LM Studio's idle time from this app, create `config.json` in this folder with `{"summary_extra_body": {"ttl": 300}}` (seconds).

To keep the PC free during the day, also look at **When to summarize** in Settings:

| Setting | What happens |
|---|---|
| Right away | Each conversation is summarized about a minute after it ends |
| Once a day | Transcripts appear right away. All summaries and the digest run in one batch at the time you pick. If the PC was off then, the batch runs when it is next on |
| Only when I ask | Nothing is summarized until you press "Summarize pending now" in Settings or "Re-summarize" on a conversation |

And under Settings > System, **Wait while the PC is busy** holds transcription and local-model summaries while other programs are using the GPU or CPU heavily (a game, a render). Recordings wait in the queue and are processed when the PC calms down.

## 5. Connect the watch

On the watch:

1. Settings > About watch > Software information. Tap **Software version** seven times to unlock Developer options.
2. Settings > Developer options. Turn on **ADB debugging** and **Wireless debugging**. The watch must be on the same Wi-Fi as the PC.
3. Tap **Wireless debugging > Pair new device**. It shows an address, a port and a six-digit code.

On the PC, use the address and port from the pairing screen, then the code:

```powershell
adb pair 192.168.1.23:37000
```

Go back one screen on the watch. It shows a second address and port for connecting (the port is different from the pairing one):

```powershell
adb connect 192.168.1.23:41000
adb devices
```

`adb devices` should list the watch as `device`.

## 6. Let the watch reach the PC (Wi-Fi mode)

`.env.example` already sets `BIND_HOST=0.0.0.0`, which lets devices on your home network reach the upload address. Windows Firewall still blocks it until you allow it. Open PowerShell **as administrator** and run:

```powershell
netsh advfirewall firewall add rule name="Voice Capture" dir=in action=allow protocol=TCP localport=8808 profile=private
```

Your Wi-Fi has to be set as a Private network in Windows for this rule to apply.

Other devices on your network can only reach the watch's upload address, and only with the token. The dashboard answers on the PC itself. To open the dashboard from your phone at home, set `DASHBOARD_ON_LAN=1` in `.env` and restart. Anyone on your Wi-Fi can then read it.

## 7. Install the watch app

Download `voice-capture-watch.apk` from this repo's Releases page into a `dist` folder here. Then:

```powershell
python tools/setup_watch.py
```

It installs the app, grants the microphone permission, and tells the watch this PC's address and token. Nothing is typed on the watch.

To build the app yourself instead: install Android Studio, then `cd watch; .\gradlew.bat assembleDebug`. The setup script finds the built file.

## 8. Try it

Open **Voice Capture** on the watch, tap the big button, talk for twenty seconds, tap it again. Within a minute or two the conversation appears in the dashboard.

Then, in the dashboard under **Settings > Voices**, enroll your own voice so your lines are labeled with your name.

When you're finished, turn Wireless debugging off on the watch. The recorder doesn't need it, and it saves some battery. Turn it back on only to install an update.

## Using it day to day

- **Start and stop.** The button in the app, the Voice Capture shortcut you can add to a watch face, or a double press of the Home key. For the double press on a Galaxy Watch: Settings > Buttons and gestures > Double press Home key > **Quick Rec**.
- **Button recordings** stop by themselves after 10 minutes of silence.
- **Auto mode** (toggle in the app) listens whenever the watch is on your wrist and records when it hears speech. Two minutes of silence ends the recording. In dashboard Settings you can limit it to weekly time windows, which is much easier on the battery.
- **Stitching.** Recordings less than 10 minutes apart are joined into one conversation unless the other voices clearly differ.
- Dashboard setting changes reach the watch within about two minutes while it is recording or listening, and the next time you open the app otherwise.
- After a watch restart or an app update, Android won't let the app reopen the microphone by itself. The watch shows "Auto mode is paused, tap to resume". Opening the app fixes it.

## Optional: record calendar meetings

In the dashboard, go to **Settings > Auto mode > When to listen**. Paste your calendar's private iCal link into **Calendar link** and press **Save link**, then tick **Record calendar meetings** and press **Save changes**. In Google Calendar the link is under Settings > your calendar > "Secret address in iCal format". Auto mode then also opens for meetings that have attendees or a video-call link.

The link is read-only and there is no sign-in to your calendar account. Treat it like a password anyway: anyone who has it can read your calendar. It is stored only in the `.env` file on your PC.

## Optional: upload from anywhere (tunnel mode)

Wi-Fi mode only uploads at home. To upload from anywhere, put the PC behind a tunnel. With Cloudflare Tunnel:

1. Route two hostnames to `http://127.0.0.1:8808`, for example `voice.example.com` (dashboard) and `voice-in.example.com` (watch uploads).
2. Protect the dashboard hostname with a Cloudflare Access policy that allows only you. **Without this, the dashboard is public.**
3. Optionally protect the upload hostname with an Access service token and put it in `.env` as `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`.
4. In `.env` set `BIND_HOST=127.0.0.1` and `INGEST_HOST=voice-in.example.com`, then `python launch.py restart`.
5. `python tools/setup_watch.py --config-only --url https://voice-in.example.com`

## Troubleshooting

| Problem | Fix |
|---|---|
| Watch says "offline, will retry" | The PC is off, the firewall rule is missing, or the PC's address changed. Run `python tools/setup_watch.py --config-only` again. Giving the PC a fixed address in your router stops this from coming back. |
| Watch says "rejected: check tokens" | The token in `.env` changed. Run `python tools/setup_watch.py --config-only`. |
| `adb devices` shows nothing or "offline" | `adb disconnect`, then `adb connect` again with the address on the Wireless debugging screen. The port changes each time debugging is switched on. |
| "More than one device" | The watch can show up twice. Pass `--serial <address:port>` to the setup script. |
| Every line is "Speaker A" | No Hugging Face token saved (Settings > Voices > Speaker labels), or the model terms weren't accepted. |
| Summaries never appear | Settings > Summaries > Save and test shows the exact error. Also check "When to summarize": on the daily or manual setting they wait on purpose. |
| Recordings sit at "queued" | "Wait while the PC is busy" is on and something is loading the GPU or CPU. Pipeline status shows the reason. |
| Dashboard says "wrong Python" | Start it with `python launch.py start`, which uses the `.venv` in this folder. |
