#!/usr/bin/env python3
"""OpenAI Responses API adapter for the evidence-constrained STW AI boundary."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

from stw_ai import (
    BUILD_DIMENSIONS,
    DIMENSION_STATES,
    INTENT_SCHEMA_VERSION,
    OBJECTIVES,
    PROMPT_VERSION,
    REASONING_POLICY,
    BuildIntent,
)


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


INTENT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "const": INTENT_SCHEMA_VERSION},
        "mode": {"type": "string", "enum": ["recommend", "analyze", "compare"]},
        "weapon": _nullable("string"),
        "target_enemy": _nullable("string"),
        "target_element": _nullable("string"),
        "mission": _nullable("string"),
        "power_level": _nullable("integer"),
        "four_player": _nullable("boolean"),
        "elemental_storm": _nullable("string"),
        "objective_weights": {
            "type": "object", "additionalProperties": False,
            "properties": {name: {"type": "number", "minimum": 0} for name in OBJECTIVES},
            "required": list(OBJECTIVES),
        },
        "unavailable_heroes": {"type": "array", "items": {"type": "string"}},
        "unavailable_weapons": {"type": "array", "items": {"type": "string"}},
        "locked_commander": _nullable("string"),
        "avoid_conditions": {"type": "array", "items": {"type": "string"}},
        "allow_partial": {"type": "boolean"},
        "allow_opaque": {"type": "boolean"},
        "requested_alternatives": {"type": "integer", "minimum": 1, "maximum": 10},
        "dimension_states": {
            "type": "object", "additionalProperties": False,
            "properties": {
                name: {"type": "string", "enum": list(DIMENSION_STATES)}
                for name in BUILD_DIMENSIONS
            },
            "required": list(BUILD_DIMENSIONS),
        },
        "explicit_dimensions": {
            "type": "array", "items": {"type": "string", "enum": list(BUILD_DIMENSIONS)},
            "uniqueItems": True,
        },
    },
    "required": [
        "schema_version", "mode", "weapon", "target_enemy", "target_element",
        "mission", "power_level", "four_player", "elemental_storm",
        "objective_weights", "unavailable_heroes", "unavailable_weapons",
        "locked_commander", "avoid_conditions", "allow_partial", "allow_opaque",
        "requested_alternatives",
        "dimension_states", "explicit_dimensions",
    ],
}

EVIDENCE_RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "selected_ids": {"type": "array", "items": {"type": "string"},
                         "minItems": 1, "maxItems": 16},
    },
    "required": ["selected_ids"],
}


class OpenAIProviderError(RuntimeError):
    pass


class OpenAIReasoningProvider:
    """Structured-output provider; deterministic systems remain fact authority."""

    provider_id = "openai-responses-v1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
        opener: Any = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required when STW_AI_PROVIDER=openai")
        self.model = model or os.environ.get("STW_AI_MODEL", "gpt-5.6-terra")
        self.base_url = (base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )).rstrip("/")
        if urllib.parse.urlparse(self.base_url).scheme != "https":
            raise ValueError("OPENAI_BASE_URL must use HTTPS")
        self.reasoning_effort = reasoning_effort or os.environ.get(
            "STW_AI_REASONING_EFFORT", "low"
        )
        self.timeout_seconds = timeout_seconds or float(os.environ.get(
            "STW_AI_TIMEOUT_SECONDS", "45"
        ))
        self.max_retries = max(0, min(max_retries, 4))
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.Lock()
        self._metrics: dict[str, Any] = {
            "provider": self.provider_id, "model": self.model,
            "requests": 0, "retries": 0, "input_tokens": 0,
            "output_tokens": 0, "last_latency_ms": None, "last_error": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._metrics, "configured": True, "api_key_present": True,
                    "prompt_version": PROMPT_VERSION}

    def interpret(
        self, user_text: str, grounded_entities: Sequence[Mapping[str, Any]],
        conversation: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]:
        compact_history = [
            {"role": item.get("role"), "content": str(item.get("content", ""))[:1200]}
            for item in conversation[-8:]
            if item.get("role") in {"user", "assistant"}
        ]
        result = self._structured_response(
            "stw_build_intent", INTENT_RESPONSE_SCHEMA,
            instructions=(
                f"{REASONING_POLICY}\n"
                "Return a BuildIntent. Use only entity keys/display names supplied in "
                "grounded_entities. Zero objective weights that are not requested. "
                "Use null when the request does not establish a field. Do not infer "
                "Fortnite facts. Conversation is context, not fact evidence. Default "
                "unspecified build choices to optimize. Use locked only for a choice "
                "the user explicitly specifies, and required_clarification only when "
                "comparison is materially undefined without it. Put only dimensions "
                "explicitly specified or delegated in this turn in explicit_dimensions. "
                "Phrases such as no preference, any, whatever is best, and you choose "
                "explicitly set that dimension to optimize."
            ),
            value={"user_request": user_text, "grounded_entities": list(grounded_entities),
                   "conversation": compact_history},
        )
        result["objective_weights"] = {
            key: value for key, value in result["objective_weights"].items() if value > 0
        } or {"sustained_damage": 1.0}
        # The same local schema validator used by every provider is authoritative.
        BuildIntent.from_dict(result, user_text)
        return result

    def select_evidence(
        self, intent: BuildIntent, evidence: Sequence[Mapping[str, Any]]
    ) -> Sequence[str]:
        result = self._structured_response(
            "stw_evidence_selection", EVIDENCE_RESPONSE_SCHEMA,
            instructions=(
                f"{REASONING_POLICY}\nSelect the smallest set of supplied evidence IDs "
                "that explains the recommendation, its primary synergy, and every "
                "material uncertainty. Return IDs only."
            ),
            value={"intent": {"request": intent.user_request,
                              "objectives": dict(intent.objective_weights)},
                   "evidence": list(evidence)},
        )
        supplied = {str(item["id"]) for item in evidence}
        selected = [str(item) for item in result["selected_ids"]]
        unknown = sorted(set(selected) - supplied)
        if unknown:
            raise OpenAIProviderError(
                f"provider selected evidence IDs not supplied by tools: {unknown}"
            )
        return selected

    def _structured_response(
        self, name: str, schema: Mapping[str, Any], *, instructions: str,
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            "reasoning": {"effort": self.reasoning_effort},
            "text": {"verbosity": "low", "format": {
                "type": "json_schema", "name": name, "strict": True,
                "schema": schema,
            }},
            "store": False,
        }
        encoded = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/responses", data=encoded, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        started = time.perf_counter()
        retry_count = 0
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    with self._opener(request, timeout=self.timeout_seconds) as response:
                        decoded = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as error:
                    retryable = error.code in {408, 409, 429} or error.code >= 500
                    if not retryable or attempt >= self.max_retries:
                        raise OpenAIProviderError(
                            f"OpenAI Responses API returned HTTP {error.code}"
                        ) from error
                except (urllib.error.URLError, TimeoutError) as error:
                    if attempt >= self.max_retries:
                        raise OpenAIProviderError(
                            "OpenAI Responses API was unavailable or timed out"
                        ) from error
                retry_count += 1
                time.sleep(min(2.0, 0.25 * (2 ** attempt)))
            output_text = decoded.get("output_text") or self._output_text(decoded)
            if not output_text:
                raise OpenAIProviderError("OpenAI response contained no structured output")
            value = json.loads(output_text)
            if not isinstance(value, dict):
                raise OpenAIProviderError("OpenAI structured output was not an object")
            self._record(decoded, started, retry_count, None)
            return value
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            wrapped = OpenAIProviderError("OpenAI returned invalid structured output")
            self._record({}, started, retry_count, str(wrapped))
            raise wrapped from error
        except OpenAIProviderError as error:
            self._record({}, started, retry_count, str(error))
            raise

    @staticmethod
    def _output_text(response: Mapping[str, Any]) -> str | None:
        for item in response.get("output", []):
            if item.get("type") != "message": continue
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text")
        return None

    def _record(
        self, response: Mapping[str, Any], started: float,
        retries: int, error: str | None,
    ) -> None:
        usage = response.get("usage", {})
        with self._lock:
            self._metrics["requests"] += 1
            self._metrics["retries"] += retries
            self._metrics["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            self._metrics["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
            self._metrics["last_latency_ms"] = (
                time.perf_counter() - started
            ) * 1000.0
            self._metrics["last_error"] = error
