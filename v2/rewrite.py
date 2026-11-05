"""Pass 1.5: ask Gemini to rewrite a line that is too long or too short for its slot.

The target is the slot's duration in seconds (TCR OUT - TCR IN). Gemini gets the character's name,
the line, how long it currently takes and how long it must take, and the character's other lines in
the episode so the rewrite stays in character. Meaning and tone must be kept, and [tags] such as
[laughs] are kept exactly as they are.

On a second attempt Gemini is also told what its earlier rewrites were and how long each one came
out when spoken, so it can correct further. The pipeline decides how many attempts to make and
which take to keep; this module only writes one rewrite per call.
"""

from dataclasses import dataclass, field
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MODEL_ID = "gemini-3.8-flash"     # single place to change the rewrite model
THINKING_LEVEL = "low"            # keeps rewrites fast and cheap; None leaves the model's default
TEMPERATURE = 0.7
MAX_CONTEXT_LINES = 40            # the character's other lines sent for personality; keeps prompts small
TIMEOUT_S = 60
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

TAG_PATTERN = re.compile(r"\[[^\[\]\r\n]+\]")

SYSTEM_INSTRUCTION = (
    "You are a dubbing script adapter. You rewrite one line of dubbed dialogue so that, spoken at a "
    "natural pace, it lasts a target duration and matches the actor's lip movement on screen.\n"
    "Rules:\n"
    "- Keep the meaning and the emotional tone of the line.\n"
    "- Keep the line in character: match how this character speaks in their other lines.\n"
    "- Write in the same language as the line.\n"
    "- Keep every [tag] in square brackets exactly as written, in the same order. Never add new tags.\n"
    "- Do not add stage directions, quotation marks, the speaker's name or notes.\n"
    "- Reply with JSON only: {\"dialogue\": \"<the rewritten line>\"}"
)


class RewriteError(RuntimeError):
    """Gemini could not produce a usable rewrite."""


@dataclass(frozen=True)
class Attempt:
    text: str
    duration_ms: int


@dataclass(frozen=True)
class RewriteRequest:
    character: str
    text: str
    duration_ms: int                       # the meter of the current take
    target_ms: int                         # TCR OUT - TCR IN
    context_lines: tuple = ()              # the character's other lines in this episode
    previous_attempts: tuple = field(default=())   # earlier rewrites of this line, oldest first


def tags(text):
    return TAG_PATTERN.findall(text)


def has_spoken_words(text):
    """False for tag-only lines such as ``[Gasps]``: there is nothing to rewrite."""
    return bool(re.search(r"\w", TAG_PATTERN.sub("", text)))


def word_count(text):
    """Spoken words, split on spaces, ignoring [tags] and stray punctuation: "[Angry] Get out!" has 2.
    Languages written without spaces (Chinese, Japanese, Thai) count a whole phrase as one word."""
    return sum(1 for word in TAG_PATTERN.sub(" ", text).split() if re.search(r"\w", word))


def seconds(ms):
    return f"{ms / 1000:.2f} s"


def describe_change(duration_ms, target_ms):
    percent = round(abs(duration_ms - target_ms) / target_ms * 100)
    if duration_ms > target_ms:
        return f"{percent}% too long: make it shorter"
    return f"{percent}% too short: make it longer"


def build_prompt(request):
    parts = [f"Character: {request.character}"]
    context = [line for line in request.context_lines if line.strip()][:MAX_CONTEXT_LINES]
    if context:
        parts.append("How this character speaks (their other lines in this episode):\n"
                     + "\n".join(f"- {line}" for line in context))
    parts.append(f"Line to rewrite:\n{request.text}")
    parts.append(f"Spoken now it lasts {seconds(request.duration_ms)}. The slot is "
                 f"{seconds(request.target_ms)}. It is {describe_change(request.duration_ms, request.target_ms)}.")
    if request.previous_attempts:
        lines = [f"- \"{attempt.text}\" was spoken in {seconds(attempt.duration_ms)}: "
                 f"{describe_change(attempt.duration_ms, request.target_ms)}"
                 for attempt in request.previous_attempts]
        parts.append("Your earlier rewrites of this line, and how long each one took when spoken:\n"
                     + "\n".join(lines) + "\nCorrect for this in the new version.")
    parts.append(f"Rewrite the line so it lasts about {seconds(request.target_ms)} when spoken at a natural pace.")
    return "\n\n".join(parts)


def request_body(prompt):
    config = {
        "temperature": TEMPERATURE,
        "responseMimeType": "application/json",
        "responseSchema": {"type": "OBJECT", "properties": {"dialogue": {"type": "STRING"}},
                           "required": ["dialogue"]},
    }
    if THINKING_LEVEL:
        config["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }


def parse_rewrite(payload, original):
    """The rewritten line from Gemini's response; raises RewriteError if it is unusable."""
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        dialogue = json.loads(text)["dialogue"]
    except (KeyError, IndexError, TypeError, ValueError):
        reason = (payload.get("promptFeedback") or {}).get("blockReason") if isinstance(payload, dict) else None
        raise RewriteError(f"Gemini returned no rewrite{f' (blocked: {reason})' if reason else ''}") from None
    if not isinstance(dialogue, str) or not has_spoken_words(dialogue):
        raise RewriteError("Gemini returned an empty rewrite")
    dialogue = " ".join(dialogue.split())
    if tags(dialogue) != tags(original):
        raise RewriteError(f"Gemini changed the [tags]: {tags(original)} became {tags(dialogue)}")
    return dialogue


def error_detail(error, api_key):
    """Gemini's error message, without the API key."""
    try:
        payload = json.loads(error.read(8192).decode("utf-8"))
        message = payload["error"].get("message", "")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError):
        return ""
    if not isinstance(message, str):
        return ""
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return " ".join(message.split())[:600]


class GeminiRewriter:
    """``rewriter(RewriteRequest) -> rewritten line``. One malformed answer is asked again once."""

    def __init__(self, api_key=None):
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("Set GEMINI_API_KEY in v2/.env or the environment")

    def __call__(self, request):
        body = json.dumps(request_body(build_prompt(request))).encode("utf-8")
        for attempt in range(2):
            try:
                return parse_rewrite(self._post(body), request.text)
            except RewriteError:
                if attempt == 1:
                    raise

    def _post(self, body):
        request = Request(API_URL.format(model=quote(MODEL_ID, safe="")), data=body, method="POST",
                          headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        try:
            with urlopen(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = error_detail(exc, self.api_key)
            reason = f": {detail}" if detail else ""
            raise RuntimeError(f"Gemini returned HTTP {exc.code} (model {MODEL_ID}){reason}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("Gemini connection failed or timed out") from exc
        except (UnicodeError, ValueError) as exc:
            raise RewriteError("Gemini returned a response that is not JSON") from exc
