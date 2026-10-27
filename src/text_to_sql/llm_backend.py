"""Pluggable hosted-LLM translator, used as a fallback for open-ended
questions the rule-based parser can't confidently handle.

This is a real OpenAI Chat-Completions-compatible HTTP client: it works
against OpenAI itself, or any self-hosted/compatible endpoint (vLLM,
Ollama's OpenAI-compatible route, Azure OpenAI, etc.) by changing
`base_url`. It is never used unless the caller explicitly constructs an
`LLMBackend` with an API key/endpoint — the CLI's default mode never makes
a network call.

The HTTP transport is injected (`http_post`), so tests can stub it out
deterministically instead of hitting a real network — the same pattern
used for other HTTP-backed integrations in this project.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol

from text_to_sql.schema import Schema

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = (
    "You translate natural-language questions into a single SQLite SELECT "
    "statement for the given schema. Reply with ONLY the SQL query, no "
    "explanation, no markdown fences, no trailing semicolon required. "
    "Always qualify column names with their table name. Use only the "
    "tables and columns given in the schema."
)


class HttpPost(Protocol):
    def __call__(self, url: str, data: bytes, headers: dict[str, str], timeout: float) -> bytes: ...


def _default_http_post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LLMBackendError(f"HTTP {exc.code} from LLM endpoint: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMBackendError(f"Network error contacting LLM endpoint: {exc}") from exc


class LLMBackendError(Exception):
    pass


def _schema_description(schema: Schema) -> str:
    lines = []
    for table in schema.tables.values():
        cols = ", ".join(f"{c.name} {c.type}" for c in table.columns.values())
        lines.append(f"TABLE {table.name}({cols})")
        for fk in table.foreign_keys:
            lines.append(f"  FOREIGN KEY {table.name}.{fk.from_column} -> {fk.to_table}.{fk.to_column}")
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


@dataclass
class LLMBackend:
    """An OpenAI Chat-Completions-compatible NL-to-SQL translator.

    api_key: required unless the endpoint doesn't need one (e.g. a local
        Ollama/vLLM server).
    base_url: defaults to OpenAI; point it at any compatible endpoint.
    http_post: injected transport, defaults to a real urllib POST. Tests
        pass a stub here instead of making a network call.
    """

    api_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = 30.0
    http_post: Callable[[str, bytes, dict[str, str], float], bytes] = _default_http_post

    def translate(self, question: str, schema: Schema) -> str:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Schema:\n{_schema_description(schema)}\n\nQuestion: {question}",
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = self.http_post(url, json.dumps(payload).encode("utf-8"), headers, self.timeout)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMBackendError(f"Could not parse LLM response as JSON: {body!r}") from exc

        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMBackendError(f"Unexpected LLM response shape: {parsed!r}") from exc

        return _strip_code_fences(content)
