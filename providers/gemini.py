"""Native Google Gemini REST adapter.

The API key is supplied only through GEMINI_API_KEY and sent in the x-goog-api-key
header. Errors deliberately omit request details and credentials.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from providers.base import AIProvider
from providers.http import encode_provider_payload, open_provider_request, read_provider_json


class GeminiProvider(AIProvider):
    name = "gemini-native"
    capabilities = ("question_generation", "question_verification", "nlu")

    def __init__(self, api_key=None, model=None, timeout=30):
        self.api_key = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")).strip()
        self.model = (model if model is not None else os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")).strip()
        if self.model.startswith("models/"):
            self.model = self.model[len("models/"):]
        self.timeout = max(1, min(int(timeout), 120))

    def health_check(self):
        return "available" if self.api_key and self.model else "not_configured"

    def _generate_json(self, instruction, payload):
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        if not self.model or any(ch in self.model for ch in "/?#"):
            raise RuntimeError("GEMINI_MODEL is invalid")

        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            + urllib.parse.quote(self.model, safe="-._")
            + ":generateContent"
        )
        prompt = instruction + "\nReturn only one valid JSON object. Input:\n" + json.dumps(payload, ensure_ascii=False)
        body = encode_provider_payload({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
            },
        }, "Gemini")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
        )
        try:
            with open_provider_request(request, timeout=self.timeout) as response:
                data = read_provider_json(response, "Gemini")
        except urllib.error.HTTPError as exc:
            raise RuntimeError("Gemini provider returned HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Gemini provider connection failed: " + type(exc).__name__) from None

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("Gemini provider returned an invalid JSON response") from None
        if not isinstance(result, dict):
            raise RuntimeError("Gemini provider response must be a JSON object")
        return result

    def generate_question(self, concept, question_type, difficulty):
        instructions = {
            "definition": "Create one definition question asking the learner to define the concept.",
            "mcq": "Create one multiple-choice question with exactly four choices and one correct answer.",
            "short_answer": "Create one short-answer educational question.",
            "long_answer": "Create one open-ended long-answer educational question.",
        }
        result = self._generate_json(
            "Create one age-appropriate educational question. " + instructions.get(question_type, "Create one educational question.")
            + " Return JSON with content, answer, explanation, and choices when applicable. Do not invent source citations.",
            {"concept": concept, "question_type": question_type, "difficulty": difficulty},
        )
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Gemini provider did not return non-empty question content")
        result["content"] = content.strip()
        result["difficulty"] = difficulty
        result["verification_status"] = "ai_generated_unverified"
        result["provider"] = self.name
        return result

    def verify_question(self, question):
        result = self._generate_json(
            "Review this question for clarity, formatting, and unsupported claims. Return JSON with check values only pass, fail, or uncertain. Do not assert factual correctness without supporting context.",
            {"question": question},
        )
        allowed = {"pass", "fail", "uncertain"}
        clean = {str(key): value for key, value in result.items() if isinstance(key, str) and value in allowed}
        return clean or {"provider_review": "uncertain"}

    def parse_command(self, raw_text):
        result = self._generate_json(
            "Classify the school-platform command. Return JSON with intent and confidence. Allowed intents: mark_attendance, generate_exam, view_classes, other. Never execute the command.",
            {"text": raw_text},
        )
        allowed = {"mark_attendance", "generate_exam", "view_classes", "other"}
        intent = result.get("intent")
        try:
            confidence = float(result.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "intent": intent if intent in allowed else "other",
            "confidence": max(0.0, min(1.0, confidence)),
        }
