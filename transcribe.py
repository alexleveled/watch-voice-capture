"""WhisperX on this PC: transcribe -> word alignment -> speaker diarization.

Models load lazily and are dropped after `model_idle_unload_minutes`, so the GPU's memory is free
whenever nothing is being transcribed. Diarization needs HF_TOKEN (pyannote is gated); without it
every segment is labeled with one speaker and the pipeline still works.

Built and tested on an NVIDIA GPU. With "device": "auto" it falls back to the CPU (int8) when
CUDA isn't available; that path is much slower and has not been tested.
"""

from __future__ import annotations

import gc
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import config

SAMPLE_RATE = 16000


@dataclass
class Result:
    duration_s: float
    language: str | None
    segments: list[dict]                       # {start_s, end_s, label, text}
    speakers: dict[str, dict] = field(default_factory=dict)  # label -> {embedding: np.ndarray|None, speech_s}


def to_wav(paths: list[Path], out: Path) -> Path:
    """Decode and join chunks (any format ffmpeg reads) into one 16 kHz mono WAV."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for p in paths:
        args += ["-i", str(p)]
    n = len(paths)
    if n == 1:
        args += ["-ac", "1", "-ar", str(SAMPLE_RATE), str(out)]
    else:
        inputs = "".join(f"[{i}:a]" for i in range(n))
        args += ["-filter_complex", f"{inputs}concat=n={n}:v=0:a=1[a]", "-map", "[a]",
                 "-ac", "1", "-ar", str(SAMPLE_RATE), str(out)]
    subprocess.run(args, check=True, capture_output=True, timeout=1800)
    return out


class Engine:
    def __init__(self):
        want = config.tunables().get("device") or "auto"
        if want == "auto":
            try:
                import torch
                want = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                want = "cpu"
        self.device = want
        self._asr = None
        self._align: dict[str, tuple] = {}
        self._diar = None
        self.last_used = 0.0

    # -- model lifecycle --
    def _whisperx(self):
        import whisperx  # heavy import, keep it lazy
        return whisperx

    def asr(self):
        if self._asr is None:
            cfg = config.tunables()
            self._asr = self._whisperx().load_model(cfg["whisper_model"], self.device,
                                                    compute_type=cfg["compute_type"] if self.device == "cuda" else "int8",
                                                    language=cfg["language"] or None)
        return self._asr

    def aligner(self, lang: str):
        if lang not in self._align:
            self._align[lang] = self._whisperx().load_align_model(language_code=lang, device=self.device)
        return self._align[lang]

    def diarizer(self):
        if self._diar is None and config.hf_token():
            from whisperx.diarize import DiarizationPipeline
            self._diar = DiarizationPipeline(model_name=config.tunables()["diarize_model"],
                                             token=config.hf_token(), device=self.device)
        return self._diar

    def loaded(self) -> bool:
        return bool(self._asr or self._align or self._diar)

    def unload(self) -> None:
        self._asr, self._align, self._diar = None, {}, None
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

    def maybe_unload(self) -> None:
        if self.loaded() and time.time() - self.last_used > config.tunables()["model_idle_unload_minutes"] * 60:
            self.unload()

    # -- work --
    def _diarize(self, audio: np.ndarray):
        d = self.diarizer()
        if d is None:
            return None, {}
        df, embs = d(audio, return_embeddings=True)
        return df, (embs or {})

    def run(self, wav: Path) -> Result:
        self.last_used = time.time()
        wx = self._whisperx()
        cfg = config.tunables()
        audio = wx.load_audio(str(wav))
        duration = len(audio) / SAMPLE_RATE
        out = self.asr().transcribe(audio, batch_size=cfg["batch_size"], language=cfg["language"] or None)
        lang = out.get("language") or cfg["language"] or "en"
        if not out.get("segments"):
            return Result(duration, lang, [])
        try:
            model_a, meta = self.aligner(lang)
            out = wx.align(out["segments"], model_a, meta, audio, self.device, return_char_alignments=False)
        except Exception:
            pass  # no aligner for this language: keep segment-level timing
        try:
            df, embs = self._diarize(audio)
        except Exception as e:  # gated repo, bad token, OOM: never lose the transcript over speaker labels
            print(f"diarization skipped: {str(e).splitlines()[0][:200]}", flush=True)
            self._diar = None
            df, embs = None, {}
        if df is not None:
            out = wx.assign_word_speakers(df, out)
        segs, speech = [], {}
        for s in out["segments"]:
            text = (s.get("text") or "").strip()
            if not any(ch.isalnum() for ch in text):
                continue  # Whisper turns noise into "." / "..." segments; drop them
            label = s.get("speaker") or "SPEAKER_00"
            segs.append({"start_s": float(s["start"]), "end_s": float(s["end"]), "label": label, "text": text})
            speech[label] = speech.get(label, 0.0) + float(s["end"]) - float(s["start"])
        speakers = {lab: {"embedding": np.asarray(embs[lab], dtype=np.float32) if lab in embs else None,
                          "speech_s": sec} for lab, sec in speech.items()}
        self.last_used = time.time()
        return Result(duration, lang, segs, speakers)

    def voiceprint(self, wav: Path) -> np.ndarray:
        """Embedding of the dominant speaker in an enrollment clip."""
        self.last_used = time.time()
        if not config.hf_token():
            raise RuntimeError("voice enrollment needs HF_TOKEN (pyannote)")
        audio = self._whisperx().load_audio(str(wav))
        df, embs = self._diarize(audio)
        if df is None or not embs:
            raise RuntimeError("no speech found in the enrollment clip")
        df = df.assign(dur=df["end"] - df["start"])
        top = df.groupby("speaker")["dur"].sum().idxmax()
        return np.asarray(embs[top], dtype=np.float32)
