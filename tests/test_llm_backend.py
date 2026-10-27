import json

from text_to_sql.llm_backend import LLMBackend, LLMBackendError


def _stub_post(expected_question: str, reply_sql: str):
    def _post(url, data, headers, timeout):
        payload = json.loads(data)
        assert "chat/completions" in url
        assert payload["messages"][-1]["content"].endswith(expected_question)
        assert headers["Authorization"] == "Bearer test-key"
        body = {"choices": [{"message": {"content": reply_sql}}]}
        return json.dumps(body).encode("utf-8")

    return _post


def test_llm_backend_translates_via_stubbed_transport(schema):
    backend = LLMBackend(
        api_key="test-key",
        http_post=_stub_post("how big is the catalog?", "SELECT COUNT(*) FROM products"),
    )
    sql = backend.translate("how big is the catalog?", schema)
    assert sql == "SELECT COUNT(*) FROM products"


def test_llm_backend_strips_markdown_code_fences(schema):
    def _post(url, data, headers, timeout):
        body = {"choices": [{"message": {"content": "```sql\nSELECT COUNT(*) FROM products\n```"}}]}
        return json.dumps(body).encode("utf-8")

    backend = LLMBackend(api_key="k", http_post=_post)
    sql = backend.translate("count products", schema)
    assert sql == "SELECT COUNT(*) FROM products"


def test_llm_backend_raises_on_malformed_response(schema):
    def _post(url, data, headers, timeout):
        return b"not json"

    backend = LLMBackend(api_key="k", http_post=_post)
    try:
        backend.translate("count products", schema)
        assert False, "expected LLMBackendError"
    except LLMBackendError:
        pass


def test_llm_backend_includes_schema_in_prompt(schema):
    captured = {}

    def _post(url, data, headers, timeout):
        payload = json.loads(data)
        captured["prompt"] = payload["messages"][-1]["content"]
        body = {"choices": [{"message": {"content": "SELECT 1"}}]}
        return json.dumps(body).encode("utf-8")

    backend = LLMBackend(api_key="k", http_post=_post)
    backend.translate("anything", schema)
    assert "TABLE customers" in captured["prompt"]
    assert "FOREIGN KEY" in captured["prompt"]
