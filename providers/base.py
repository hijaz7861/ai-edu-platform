"""
Pluggable AI provider interface — §38 made concrete.

IMPORTANT / HONEST LABEL: StubProvider below is a deterministic, offline
placeholder. It does NOT call a real LLM (this sandbox has no network access).
It exists so the surrounding architecture — provider registry, fallback by
priority, health status — is real and testable, and so a real provider
(Anthropic, OpenAI, a local model) can be dropped in later by implementing
the same AIProvider interface, without touching any calling code.
"""
from abc import ABC, abstractmethod
import os


class AIProvider(ABC):
    name = "base"
    capabilities = ()

    @abstractmethod
    def generate_question(self, concept: dict, question_type: str, difficulty: str) -> dict:
        ...

    @abstractmethod
    def verify_question(self, question: dict) -> dict:
        ...

    @abstractmethod
    def parse_command(self, raw_text: str) -> dict:
        ...

    def health_check(self) -> str:
        return "available"


class StubProvider(AIProvider):
    """Deterministic, offline, rule-based — NOT a real LLM. Real question wording,
    real verification reasoning, and real NLU require wiring in an actual provider
    over the network in the deployed environment."""

    name = "stub-offline-v1"
    capabilities = ("question_generation", "question_verification", "nlu")

    def generate_question(self, concept: dict, question_type: str, difficulty: str) -> dict:
        name = concept.get("name", "this concept")
        if question_type == "definition":
            content = f"Define '{name}' in your own words."
        elif question_type == "mcq":
            content = f"Which of the following best describes '{name}'?"
        else:
            content = f"Explain '{name}' and give one real-world example."
        return {
            "content": content,
            "difficulty": difficulty,
            "verification_status": "ai_generated",
            "provider": self.name,
        }

    def verify_question(self, question: dict) -> dict:
        content = (question.get("content") or "").strip()
        checks = {
            "formatting": "pass" if content.endswith(("?", ".", ":")) else "fail",
            # A real definition prompt like "Define mitosis." is 2 words and legitimately
            # unambiguous; genuinely thin content like a bare "X?" is what this should catch.
            "ambiguity": "pass" if len(content.split()) >= 2 else "uncertain",
        }
        return checks

    def parse_command(self, raw_text: str) -> dict:
        text = raw_text.lower()
        if "attendance" in text or "حاضری" in raw_text:
            return {"intent": "mark_attendance", "confidence": 0.6}
        if "test" in text or "exam" in text:
            return {"intent": "generate_exam", "confidence": 0.6}
        if "classes" in text or "class" in text:
            return {"intent": "view_classes", "confidence": 0.5}
        return {"intent": "other", "confidence": 0.2}


def get_default_registry():
    """Return configured providers and the deterministic offline fallback."""
    registry = {"stub-offline-v1": StubProvider()}

    mistral_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    mistral_agent_id = os.environ.get("MISTRAL_AGENT_ID", "").strip()

    if mistral_key and mistral_agent_id:
        from providers.mistral_agent import MistralAgentProvider

        provider = MistralAgentProvider(
            api_key=mistral_key,
            agent_id=mistral_agent_id,
        )
        registry[provider.name] = provider

    api_key = os.environ.get("AI_API_KEY", "").strip()
    base_url = os.environ.get("AI_BASE_URL", "").strip()
    model = os.environ.get("AI_MODEL", "").strip()

    if api_key and base_url and model:
        provider = OpenAICompatibleProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        registry[provider.name] = provider

    return registry


class OpenAICompatibleProvider(AIProvider):
    """Provider for OpenAI-compatible chat-completions APIs (network/API key required).

    Configure AI_API_KEY, AI_BASE_URL (optional; defaults to api.openai.com/v1),
    and AI_MODEL. Never log the API key. This provider is opt-in and is not the default.
    """
    name = "openai-compatible"
    capabilities = ("question_generation", "question_verification", "nlu")

    def __init__(self, api_key=None, base_url=None, model=None, timeout=30):
        import os
        self.api_key = api_key or os.environ.get("AI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.environ.get("AI_MODEL", "gpt-4o-mini")
        self.timeout = max(1, min(int(timeout), 120))

    def health_check(self):
        return "available" if self.api_key and self.model and self.base_url.startswith("https://") else "not_configured"

    def _complete_json(self, instruction, payload):
        import json
        import urllib.error
        import urllib.request
        if not self.api_key:
            raise RuntimeError("AI_API_KEY is not configured")
        if not self.base_url.startswith("https://") and not os.environ.get("AI_ALLOW_INSECURE_HTTP") == "1":
            raise RuntimeError("AI_BASE_URL must use HTTPS")
        body = json.dumps({
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instruction + " Return only a valid JSON object."},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Do not include request headers or credentials in errors.
            raise RuntimeError("AI provider returned HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError("AI provider connection failed: " + type(exc).__name__) from None
        try:
            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError):
            raise RuntimeError("AI provider returned an invalid JSON response") from None
        if not isinstance(result, dict):
            raise RuntimeError("AI provider response must be a JSON object")
        return result

    def generate_question(self, concept, question_type, difficulty):
        type_instructions = {
            "definition": (
                "Create a definition question. The content must ask the learner to define "
                "the supplied concept. Do not turn it into a multiple-choice question."
            ),
            "mcq": (
                "Create exactly one multiple-choice question. Return exactly four choices "
                "and an answer that exactly matches one of the choices."
            ),
            "short_answer": (
                "Create one short-answer question that can be answered in a few sentences."
            ),
            "long_answer": (
                "Create one open-ended long-answer question requiring a detailed explanation."
            ),
        }
        instruction = (
            "Create one age-appropriate educational question. Do not invent source citations. "
            + type_instructions.get(
                question_type,
                "Create one educational question of the requested type.",
            )
            + " Return a JSON object with content, difficulty, answer, explanation, and "
            "choices when applicable. Keep the requested question type."
        )
        result = self._complete_json(
            instruction,
            {"concept": concept, "question_type": question_type, "difficulty": difficulty},
        )
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("AI provider did not return non-empty question content")
        result["content"] = content.strip()
        result["difficulty"] = difficulty
        result["verification_status"] = "ai_generated_unverified"
        result["provider"] = self.name
        return result

    def verify_question(self, question):
        result = self._complete_json(
            "Review the educational question for clarity and formatting. Return a JSON object "
            "whose check values are only pass, fail, or uncertain; do not claim factual correctness "
            "unless the supplied context supports it.",
            {"question": question},
        )
        allowed = {"pass", "fail", "uncertain"}
        clean = {str(k): v for k, v in result.items() if isinstance(k, str) and v in allowed}
        return clean or {"provider_review": "uncertain"}

    def parse_command(self, raw_text):
        result = self._complete_json(
            "Classify a school platform command. Return intent and confidence. "
            "Allowed intents: mark_attendance, generate_exam, view_classes, other. "
            "Never execute the command.",
            {"text": raw_text},
        )
        allowed = {"mark_attendance", "generate_exam", "view_classes", "other"}
        intent = result.get("intent")
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return {"intent": intent if intent in allowed else "other", "confidence": max(0.0, min(1.0, confidence))}
