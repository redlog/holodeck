import csv
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

from google import genai
from google.genai import types

def _log(msg):
    print(f"[AGENT] {msg}", file=sys.stderr, flush=True)


def _now_ts():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


class BaseAgent:
    def __init__(self, model, temperature=0.9, game_dir=None):
        self._model = model
        self._temperature = temperature
        self._game_dir = Path(game_dir) if game_dir else None
        self._result_queue = Queue()
        self._busy = False
        self._client = self._init_client()

        # Read debug flag here so it reflects .env at agent creation time.
        self._debug = os.getenv("DEBUG", "false").strip().lower() in ("1", "true", "yes")

    # ------------------------------------------------------------------ #
    # Token logging
    # ------------------------------------------------------------------ #

    def _log_tokens(self, context, tokens_in, tokens_out, tokens_cached=0, ts=None):
        if not self._game_dir:
            return
        try:
            ts = ts or _now_ts()
            log_path = self._game_dir / "token_log.csv"
            write_header = not log_path.exists()
            with log_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["timestamp", "context", "model", "tokens_in", "tokens_cached", "tokens_out"])
                w.writerow([
                    ts,
                    context or "",
                    self._model or "",
                    tokens_in,
                    tokens_cached,
                    tokens_out,
                ])
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # AI prompt/response logging (debug mode only)
    # ------------------------------------------------------------------ #

    def _log_ai(self, ts, context, label, content):
        """Write a prompt or response to <game_dir>/ai_log/ when DEBUG=true."""
        if not self._debug or not self._game_dir:
            return
        try:
            ai_log_dir = self._game_dir / "ai_log"
            ai_log_dir.mkdir(exist_ok=True)
            ts_safe = ts.replace(":", "-")
            ctx_safe = re.sub(r"[^a-zA-Z0-9_-]", "_", context or "unknown")[:40].strip("_")
            filename = f"{ts_safe}_{ctx_safe}_{label}.txt"
            (ai_log_dir / filename).write_text(content, encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _format_text_prompt(system_prompt, contents):
        """Render system prompt + message history as readable plain text."""
        parts = []
        if system_prompt:
            parts.append(f"=== SYSTEM ===\n{system_prompt}")
        parts.append("=== MESSAGES ===")
        for msg in contents:
            if isinstance(msg, str):
                parts.append(f"[user]: {msg}")
            elif isinstance(msg, dict):
                role = msg.get("role", "?")
                texts = []
                for p in (msg.get("parts") or []):
                    if isinstance(p, dict):
                        t = p.get("text")
                        if t:
                            texts.append(t)
                        elif p.get("inline_data"):
                            texts.append("<image data>")
                    elif isinstance(p, str):
                        texts.append(p)
                    else:
                        texts.append(f"<{type(p).__name__}>")
                parts.append(f"[{role}]: {''.join(texts)}")
            else:
                parts.append(f"<{type(msg).__name__}>")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #

    def _init_client(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your_key_here":
            _log("WARNING: No GEMINI_API_KEY set")
            return None
        return genai.Client(api_key=api_key)

    @property
    def connected(self):
        return self._client is not None

    @property
    def busy(self):
        return self._busy

    def poll_result(self):
        try:
            return self._result_queue.get_nowait()
        except Empty:
            return None

    def _run_threaded(self, fn, *args):
        self._busy = True
        thread = threading.Thread(target=self._thread_wrapper, args=(fn, *args), daemon=True)
        thread.start()

    def _thread_wrapper(self, fn, *args):
        try:
            fn(*args)
        finally:
            self._busy = False

    @staticmethod
    def _safety_off():
        return [
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_CIVIC_INTEGRITY", threshold="OFF"),
        ]

    # ------------------------------------------------------------------ #
    # API calls
    # ------------------------------------------------------------------ #

    def _call_text(self, system_prompt, contents, response_mime="application/json", context="", cached_content=None):
        ts = _now_ts()
        self._log_ai(ts, context, "input", self._format_text_prompt(system_prompt, contents))

        if cached_content:
            config = types.GenerateContentConfig(
                cached_content=cached_content,
                temperature=self._temperature,
                response_mime_type=response_mime,
                safety_settings=self._safety_off(),
            )
        else:
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=self._temperature,
                response_mime_type=response_mime,
                safety_settings=self._safety_off(),
            )
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        usage = response.usage_metadata
        self._log_tokens(
            context,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
            getattr(usage, "cached_content_token_count", 0) or 0,
            ts=ts,
        )
        self._log_ai(ts, context, "output", response.text or "")
        return response.text

    def _call_image(self, prompt, reference_images=None, aspect_ratio="16:9",
                    context="", model=None, negative_prompt=None):
        # The chosen model dictates which API path we take — no silent
        # fallbacks. Imagen models use the Imagen image API (which honors
        # aspect_ratio and negative_prompt but takes no reference image); Gemini
        # image models use generate_content (which accepts reference images for
        # editing but ignores aspect_ratio/negative_prompt).
        # `model` lets a caller override the agent's default per call, e.g. to
        # paint from scratch with Imagen but edit an existing image with Gemini.
        image_model = model or self._model
        ts = _now_ts()
        self._log_ai(ts, context, "input", prompt)

        if image_model.startswith("imagen"):
            def _imagen(neg):
                cfg_kwargs = dict(number_of_images=1, aspect_ratio=aspect_ratio)
                if neg:
                    cfg_kwargs["negative_prompt"] = neg
                return self._client.models.generate_images(
                    model=image_model,
                    prompt=prompt,
                    config=types.GenerateImagesConfig(**cfg_kwargs),
                )

            try:
                response = _imagen(negative_prompt)
            except Exception as e:
                # Some Imagen versions (e.g. imagen-4.0) reject negative_prompt.
                # Degrade gracefully rather than failing the whole image.
                if negative_prompt and "negative" in str(e).lower():
                    _log(f"{image_model} rejected negative_prompt; retrying without it")
                    response = _imagen(None)
                else:
                    raise
            self._log_tokens(context, 0, 0, ts=ts)
            if response.generated_images:
                self._log_ai(ts, context, "output", f"[image generated via {image_model}]")
                return response.generated_images[0].image.image_bytes
            self._log_ai(ts, context, "output", "[no image returned]")
            return None

        contents = []
        if reference_images:
            for ref_bytes in reference_images:
                contents.append(types.Part.from_bytes(data=ref_bytes, mime_type="image/png"))
        contents.append(prompt)

        response = self._client.models.generate_content(
            model=image_model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["image", "text"],
                safety_settings=self._safety_off(),
            ),
        )
        usage = response.usage_metadata
        self._log_tokens(
            context,
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
            getattr(usage, "cached_content_token_count", 0) or 0,
            ts=ts,
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                self._log_ai(ts, context, "output", f"[image generated via {image_model}]")
                return part.inline_data.data
        self._log_ai(ts, context, "output", "[no image returned]")
        return None
