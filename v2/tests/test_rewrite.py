"""Pass 1.5 Gemini rewrite, with a fake Gemini: no live API calls (standard library only)."""

import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from v2.rewrite import (MODEL_ID, Attempt, GeminiRewriter, RewriteError, RewriteRequest, build_prompt,
                        has_spoken_words, parse_rewrite, request_body)


def gemini_reply(dialogue=None, raw=None):
    text = raw if raw is not None else json.dumps({"dialogue": dialogue})
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def http_response(payload):
    response = Mock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


REQUEST = RewriteRequest(character="RAVI", text="[angry] I told you a hundred times, never come back here again!",
                         duration_ms=4350, target_ms=3000,
                         context_lines=("Get out of my house.", "", "You think I'm scared of you?"))


class PromptTests(unittest.TestCase):
    def test_prompt_gives_durations_in_seconds_direction_and_character_context(self):
        prompt = build_prompt(REQUEST)
        self.assertIn("Character: RAVI", prompt)
        self.assertIn("- Get out of my house.\n- You think I'm scared of you?", prompt)   # blank lines dropped
        self.assertIn(REQUEST.text, prompt)
        self.assertIn("lasts 4.35 s. The slot is 3.00 s. It is 45% too long: make it shorter.", prompt)
        self.assertIn("lasts about 3.00 s", prompt)
        self.assertNotIn("earlier rewrites", prompt)
        self.assertNotIn("syllable", prompt.lower())

    def test_short_lines_are_asked_to_be_longer(self):
        prompt = build_prompt(RewriteRequest("A", "No.", duration_ms=600, target_ms=2000))
        self.assertIn("70% too short: make it longer", prompt)

    def test_second_attempt_is_told_how_the_first_came_out(self):
        second = RewriteRequest(**{**REQUEST.__dict__,
                                   "previous_attempts": (Attempt("[angry] Never come back here again!", 3600),)})
        prompt = build_prompt(second)
        self.assertIn('- "[angry] Never come back here again!" was spoken in 3.60 s: 20% too long: make it shorter',
                      prompt)

    def test_context_is_capped(self):
        many = RewriteRequest("A", "Hello there.", 500, 2000, context_lines=tuple(f"line {i}" for i in range(100)))
        prompt = build_prompt(many)
        self.assertIn("line 39", prompt)
        self.assertNotIn("line 40", prompt)

    def test_request_asks_for_json_with_low_thinking(self):
        body = request_body("prompt")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertIn("same language", body["systemInstruction"]["parts"][0]["text"])


class ParseTests(unittest.TestCase):
    def test_rewrite_is_returned_with_whitespace_tidied(self):
        reply = gemini_reply("[angry]  Never   come back here!\n")
        self.assertEqual(parse_rewrite(reply, REQUEST.text), "[angry] Never come back here!")

    def test_tags_must_be_kept_exactly(self):
        for changed in ("Never come back here!", "[furious] Never come back!", "[angry] Go! [shouts]"):
            with self.subTest(changed=changed), self.assertRaisesRegex(RewriteError, "tags"):
                parse_rewrite(gemini_reply(changed), REQUEST.text)

    def test_unusable_replies_are_errors(self):
        for reply in (gemini_reply(raw="not json"), gemini_reply(""), gemini_reply("[angry]"),
                      {"candidates": []}, {"promptFeedback": {"blockReason": "SAFETY"}}):
            with self.subTest(reply=reply), self.assertRaises(RewriteError):
                parse_rewrite(reply, REQUEST.text)

    def test_thought_parts_are_ignored(self):
        reply = {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True}, {"text": json.dumps({"dialogue": "[angry] Out!"})}]}}]}
        self.assertEqual(parse_rewrite(reply, REQUEST.text), "[angry] Out!")

    def test_tag_only_lines_have_nothing_to_rewrite(self):
        self.assertFalse(has_spoken_words("[Gasps]"))
        self.assertFalse(has_spoken_words("[sighs] [laughs] ..."))
        self.assertTrue(has_spoken_words("[Gasps] Oh no."))


class ClientTests(unittest.TestCase):
    def test_request_goes_to_the_model_with_the_key_in_a_header(self):
        with patch("v2.rewrite.urlopen", return_value=http_response(gemini_reply("[angry] Never come back!"))) as call:
            result = GeminiRewriter(api_key="test-key")(REQUEST)
        self.assertEqual(result, "[angry] Never come back!")
        sent = call.call_args.args[0]
        self.assertIn(f"/models/{MODEL_ID}:generateContent", sent.full_url)
        self.assertNotIn("test-key", sent.full_url)
        self.assertEqual(sent.get_header("X-goog-api-key"), "test-key")
        self.assertIn("Line to rewrite", json.loads(sent.data)["contents"][0]["parts"][0]["text"])

    def test_one_malformed_answer_is_asked_again(self):
        replies = [http_response(gemini_reply("No tags here")), http_response(gemini_reply("[angry] Out!"))]
        with patch("v2.rewrite.urlopen", side_effect=replies) as call:
            self.assertEqual(GeminiRewriter(api_key="k")(REQUEST), "[angry] Out!")
        self.assertEqual(call.call_count, 2)

    def test_two_malformed_answers_raise(self):
        replies = [http_response(gemini_reply(raw="x")), http_response(gemini_reply(raw="y"))]
        with patch("v2.rewrite.urlopen", side_effect=replies), self.assertRaises(RewriteError):
            GeminiRewriter(api_key="k")(REQUEST)

    def test_http_errors_are_not_retried_and_hide_the_key(self):
        body = json.dumps({"error": {"code": 400, "message": "API key test-key not valid"}}).encode()
        error = HTTPError("https://generativelanguage.googleapis.com", 400, "Bad Request", {}, io.BytesIO(body))
        with patch("v2.rewrite.urlopen", side_effect=error) as call:
            with self.assertRaises(RuntimeError) as caught:
                GeminiRewriter(api_key="test-key")(REQUEST)
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(call.call_count, 1)

    def test_missing_key(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}), self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
            GeminiRewriter()


if __name__ == "__main__":
    unittest.main()
