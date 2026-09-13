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
    return {"stub-offline-v1": StubProvider()}
