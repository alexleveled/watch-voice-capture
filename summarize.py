"""Summaries, with a choice of AI provider (dashboard Settings > Summaries).

  claude_subscription   Claude Agent SDK signed in with `claude login`. Never bills API credits:
                        ANTHROPIC_API_KEY is removed before the CLI starts, and a call is refused
                        if the CLI still reports an API key as its credential source.
  codex_subscription    OpenAI Codex CLI signed in with a ChatGPT account (`codex login`), run headless.
  openai_compatible     Any /chat/completions endpoint: OpenAI, Ollama, LM Studio, OpenRouter, Gemini,
                        Anthropic's compatibility endpoint. Base URL + model + optional key.

Every provider gets the same prompt and must return the same JSON shape; `_normalize` repairs
small-model output (missing keys, strings where lists belong) so the dashboard never breaks.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

import config

PROVIDERS = ("claude_subscription", "codex_subscription", "openai_compatible")
DEFAULT_MODEL = {"claude_subscription": "sonnet", "codex_subscription": "", "openai_compatible": ""}


class SummaryError(RuntimeError):
    pass


CONV_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "key_points", "decisions", "action_items", "people", "topics"],
    "properties": {
        "title": {"type": "string", "description": "3-8 word title naming what was discussed"},
        "summary": {"type": "string", "description": "2-4 plain sentences: what was discussed and where it landed"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["item", "owner", "due"],
            "properties": {"item": {"type": "string"}, "owner": {"type": ["string", "null"]},
                           "due": {"type": ["string", "null"]}}}},
        "people": {"type": "array", "items": {"type": "string"}},
        "topics": {"type": "array", "items": {"type": "string"}},
    },
}

DAY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "summary", "key_points", "action_items", "people"],
    "properties": {
        "headline": {"type": "string", "description": "one line, under 90 chars, the day at a glance"},
        "summary": {"type": "string", "description": "a short paragraph covering the day's conversations"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "action_items": CONV_SCHEMA["properties"]["action_items"],
        "people": {"type": "array", "items": {"type": "string"}},
    },
}

CONV_SYSTEM = """You summarize one recorded conversation for {who}, who wore the recorder.
The transcript comes from automatic speech-to-text with automatic speaker labels, so expect
mis-heard words and occasional wrong speaker turns; infer sensibly, never invent facts.
Speaker names come from voiceprints: {self_rule} "Speaker A/B/..." are people not yet named.
Speaker labels can be missing entirely (one label for everyone): then describe it as a conversation
and attribute points to people only when the transcript itself makes it clear.
Write plainly and concretely: names, numbers, dates, what was agreed. No filler.
Only list decisions and action items that were actually stated. Empty lists are fine.
Return only the JSON object."""

DAY_SYSTEM = """You write the end-of-day digest for {who} from the summaries of the conversations they recorded today.
Pull out what matters: decisions, commitments, follow-ups, notable info, who they talked to.
Merge duplicates across conversations. Plain, concrete, no filler. Return only the JSON object."""


def _conv_system(wearer: str | None) -> str:
    if wearer:
        rule = (f'"{wearer}" is the wearer. If no line is labeled {wearer}, do not guess which speaker '
                f"they are; describe the speakers as labeled.")
        return CONV_SYSTEM.format(who=wearer, self_rule=rule)
    return CONV_SYSTEM.format(who="the person", self_rule="the wearer has not enrolled a voice, so do not guess which speaker they are.")


# ---- output repair --------------------------------------------------------------

def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        out = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise SummaryError(f"expected JSON, got {text[:200]!r}")
        try:
            out = json.loads(text[i:j + 1])
        except json.JSONDecodeError as e:
            raise SummaryError(f"expected JSON, got {text[:200]!r}") from e
    if not isinstance(out, dict):
        raise SummaryError("expected a JSON object")
    return out


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]


def _actions(v) -> list[dict]:
    out = []
    for a in v if isinstance(v, list) else []:
        if isinstance(a, str) and a.strip():
            out.append({"item": a.strip(), "owner": None, "due": None})
        elif isinstance(a, dict) and str(a.get("item") or a.get("task") or "").strip():
            out.append({"item": str(a.get("item") or a.get("task")).strip(),
                        "owner": (str(a["owner"]).strip() or None) if a.get("owner") else None,
                        "due": (str(a["due"]).strip() or None) if a.get("due") else None})
    return out


def _normalize(out: dict, schema: dict) -> dict:
    clean = {}
    for key, spec in schema["properties"].items():
        v = out.get(key)
        if key == "action_items":
            clean[key] = _actions(v)
        elif spec["type"] == "array":
            clean[key] = _str_list(v)
        else:
            clean[key] = str(v).strip() if isinstance(v, (str, int, float)) else ""
    if not (clean.get("summary") or clean.get("title") or clean.get("headline")):
        raise SummaryError("the model returned an empty summary")
    return clean


# ---- provider: Claude subscription ----------------------------------------------

async def _claude(prompt: str, system: str, schema: dict, model: str) -> dict:
    try:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, TextBlock, query
    except ImportError as e:
        raise SummaryError("claude-agent-sdk is not installed (pip install claude-agent-sdk)") from e
    os.environ.pop("ANTHROPIC_API_KEY", None)
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    opts = ClaudeAgentOptions(
        model=model or DEFAULT_MODEL["claude_subscription"], system_prompt=system, tools=[], allowed_tools=[],
        setting_sources=[], permission_mode="dontAsk", max_turns=4,
        output_format={"type": "json_schema", "schema": schema}, cwd=tempfile.gettempdir())
    result, texts = None, []
    async for m in query(prompt=prompt, options=opts):
        if isinstance(m, SystemMessage) and m.subtype == "init":
            src = m.data.get("apiKeySource")
            if src not in (None, "none"):
                raise SummaryError(f"CLI reports API key source {src!r}; refusing to bill API credits")
        elif isinstance(m, AssistantMessage):
            texts += [b.text for b in m.content if isinstance(b, TextBlock)]
        elif isinstance(m, ResultMessage):
            result = m
    if result is None:
        raise SummaryError("no result from Claude")
    if result.is_error:
        raise SummaryError(f"{result.subtype}: {result.result}")
    out = getattr(result, "structured_output", None)
    return out if isinstance(out, dict) else _extract_json(result.result or "\n".join(texts))


# ---- provider: Codex CLI on a ChatGPT subscription ------------------------------

def _codex(prompt: str, system: str, schema: dict, model: str) -> dict:
    exe = shutil.which("codex")
    if not exe:
        raise SummaryError("Codex CLI not found. Install it (npm i -g @openai/codex) and run `codex login`.")
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}  # subscription only
    with tempfile.TemporaryDirectory() as tmp:
        schema_path, out_path = Path(tmp) / "schema.json", Path(tmp) / "out.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        cmd = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--output-schema", str(schema_path), "--output-last-message", str(out_path)]
        if model:
            cmd += ["--model", model]
        cmd.append("-")   # prompt on stdin
        try:
            p = subprocess.run(cmd, input=f"{system}\n\n{prompt}", capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=tmp, env=env, timeout=600)
        except subprocess.TimeoutExpired as e:
            raise SummaryError("Codex CLI timed out after 10 minutes") from e
        text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        if p.returncode != 0 or not text.strip():
            tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
            raise SummaryError(f"Codex CLI failed (exit {p.returncode}): {' | '.join(tail)[:400]}")
        return _extract_json(text)


# ---- provider: any OpenAI-compatible endpoint -----------------------------------

def _compatible(prompt: str, system: str, schema: dict, model: str, base_url: str) -> dict:
    if not base_url or not model:
        raise SummaryError("set a base URL and a model name in Settings > Summaries")
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.summary_api_key():
        headers["Authorization"] = f"Bearer {config.summary_api_key()}"
    keys = ", ".join(schema["required"])
    messages = [{"role": "system", "content": f"{system}\n\nJSON keys: {keys}.\nJSON schema:\n{json.dumps(schema)}"},
                {"role": "user", "content": prompt}]
    # Strict schema first; endpoints that don't support it get plain JSON mode, then no hint at all.
    formats = [{"type": "json_schema", "json_schema": {"name": "summary", "strict": True, "schema": schema}},
               {"type": "json_object"}, None]
    last = ""
    for fmt in formats:
        body = {"model": model, "messages": messages}
        if fmt:
            body["response_format"] = fmt
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=600)
        except httpx.HTTPError as e:
            raise SummaryError(f"can't reach {url}: {e}") from e
        if r.status_code in (400, 404, 415, 422, 501) and fmt is not None:
            last = r.text[:300]
            continue
        if r.status_code >= 400:
            raise SummaryError(f"{url} answered {r.status_code}: {r.text[:300]}")
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise SummaryError(f"unexpected response from {url}: {r.text[:200]}") from e
        if isinstance(content, list):   # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return _extract_json(content or "")
    raise SummaryError(f"{url} rejected the request: {last}")


# ---- entry points ---------------------------------------------------------------

def _run(prompt: str, system: str, schema: dict, cfg: dict) -> dict:
    provider = cfg.get("provider") or "claude_subscription"
    model = (cfg.get("model") or "").strip()
    if provider == "claude_subscription":
        out = asyncio.run(_claude(prompt, system, schema, model))
    elif provider == "codex_subscription":
        out = _codex(prompt, system, schema, model)
    elif provider == "openai_compatible":
        out = _compatible(prompt, system, schema, model, (cfg.get("base_url") or "").strip())
    else:
        raise SummaryError(f"unknown summary provider {provider!r}")
    return _normalize(out, schema)


def conversation(transcript: str, when: str, duration_min: float, trigger: str, meeting: str | None,
                 cfg: dict, wearer: str | None = None) -> dict:
    head = f"Recorded {when}, {duration_min:.0f} min, trigger: {trigger}."
    if meeting:
        head += f" Calendar meeting at that time: {meeting!r}."
    # ~150k chars is far beyond any realistic single conversation; trim the middle if it ever happens.
    if len(transcript) > 150_000:
        transcript = transcript[:75_000] + "\n[... middle trimmed ...]\n" + transcript[-75_000:]
    return _run(f"{head}\n\nTranscript:\n{transcript}", _conv_system(wearer), CONV_SCHEMA, cfg)


def day(day_label: str, items: list[dict], cfg: dict, wearer: str | None = None) -> dict:
    body = "\n\n".join(
        f"[{i['time']}] {i['title']}\n{i['summary']}\nKey points: {'; '.join(i['key_points'])}\n"
        f"Action items: {'; '.join(a['item'] + (' (' + a['owner'] + ')' if a.get('owner') else '') for a in i['action_items'])}\n"
        f"People: {', '.join(i['people'])}" for i in items)
    return _run(f"Day: {day_label}\n\nConversations:\n\n{body}", DAY_SYSTEM.format(who=wearer or "the wearer"), DAY_SCHEMA, cfg)


TEST_TRANSCRIPT = """Speaker A: Did the plumber call back about the kitchen sink?
Speaker B: Yes, he can come Thursday at nine. It's a hundred and eighty dollars for the new valve.
Speaker A: Fine, book it. I'll work from home that morning to let him in.
Speaker B: Okay, I'll confirm with him tonight and send you his number."""


def test(cfg: dict) -> dict:
    """One tiny summary through the chosen provider, for the Settings "Test" button."""
    t0 = time.time()
    s = conversation(TEST_TRANSCRIPT, "today", 1, "manual", None, cfg)
    return {"ok": True, "seconds": round(time.time() - t0, 1), "title": s["title"], "summary": s["summary"],
            "action_items": len(s["action_items"])}


_status_cache: dict[str, tuple[float, str | None]] = {}


def status(cfg: dict) -> tuple[str, str | None]:
    """(label for the dashboard, problem or None). Cheap checks only, cached 10 minutes."""
    provider = cfg.get("provider") or "claude_subscription"
    if provider == "openai_compatible":
        if not (cfg.get("base_url") and cfg.get("model")):
            return "OpenAI-compatible (not set up)", "summaries need a base URL and model in Settings"
        return f"{cfg['model']} at {cfg['base_url']}", None
    hit = _status_cache.get(provider)
    if hit and time.time() - hit[0] < 600:
        problem = hit[1]
    else:
        problem = None
        if provider == "codex_subscription":
            if not shutil.which("codex"):
                problem = "Codex CLI is not installed: summaries are paused"
        else:
            exe = shutil.which("claude")
            try:
                st = json.loads(subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=20).stdout) if exe else {}
            except Exception:
                st = {}
            if not st.get("loggedIn"):
                problem = "Claude is not signed in (run `claude login`): summaries are paused"
        _status_cache[provider] = (time.time(), problem)
    label = "Codex CLI (ChatGPT subscription)" if provider == "codex_subscription" else "Claude subscription"
    return label, problem
