import csv
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv(override=True)

def _log(msg):
    print(f"[AGENT] {msg}", file=sys.stderr, flush=True)


class BaseAgent:
    def __init__(self, model, temperature=0.9, game_dir=None):
        self._model = model
        self._temperature = temperature
        self._game_dir = Path(game_dir) if game_dir else None
        self._result_queue = Queue()
        self._busy = False
        self._client = self._init_client()

    def _log_tokens(self, context, tokens_in, tokens_out, tokens_cached=0):
        if not self._game_dir:
            return
        try:
            log_path = self._game_dir / "token_log.csv"
            write_header = not log_path.exists()
            with log_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["timestamp", "context", "model", "tokens_in", "tokens_cached", "tokens_out"])
                w.writerow([
                    datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    context or "",
                    self._model or "",
                    tokens_in,
                    tokens_cached,
                    tokens_out,
                ])
        except Exception:
            pass

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

    def _call_text(self, system_prompt, contents, response_mime="application/json", context="", cached_content=None):
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
        )
        return response.text

    def _call_image(self, prompt, reference_images=None, aspect_ratio="16:9", context=""):
        # The agent's self._model dictates which API path we take — no
        # silent fallbacks. Imagen models use the Imagen image API (which
        # honors aspect_ratio); Gemini image models use generate_content.
        image_model = self._model

        if image_model.startswith("imagen"):
            response = self._client.models.generate_images(
                model=image_model,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                ),
            )
            self._log_tokens(context, 0, 0)
            if response.generated_images:
                return response.generated_images[0].image.image_bytes
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
        )
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                return part.inline_data.data
        return None
