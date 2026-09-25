"""Tests never call OpenAI: call_llm is replaced with a fake.

Unit tests run anywhere. The integration test needs real Postgres + Redis and
runs when DATABASE_URL and REDIS_URL are set (as they are in CI).
"""


import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app import graph as graph_module
from app import main
from app.graph import build_graph


def fake_llm(messages: list[dict]) -> str:
    # Echo the last message and how much history the bot could see.
    return f"echo: {messages[-1]['content']} (saw {len(messages)} messages)"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(graph_module, "call_llm", fake_llm)
    app = main.create_app()
    return TestClient(app)


@pytest.fixture
def in_memory(monkeypatch):
    """Swap Postgres for an in-memory checkpointer and turn off the Redis rate limit."""
    graph = build_graph(InMemorySaver())
    monkeypatch.setattr(main, "get_graph", lambda: graph)
    monkeypatch.setattr(main, "rate_limited", lambda thread_id: False)
    monkeypatch.setattr(main, "count_message", lambda: None)


# ---------- unit tests (no services needed) ----------

def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


def test_health_can_be_forced_unhealthy(client, monkeypatch):
    monkeypatch.setenv("FORCE_UNHEALTHY", "1")
    assert client.get("/health").status_code == 503


def test_index_reports_version(client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc123")
    assert client.get("/").json()["version"] == "abc123"


def test_chat_requires_a_message(client):
    assert client.post("/chat", json={}).status_code == 400
    assert client.post("/chat", json={"message": "   "}).status_code == 400


def test_chat_rejects_bad_thread_id(client, in_memory):
    r = client.post("/chat", json={"message": "hi", "thread_id": "../../etc"})
    assert r.status_code == 400


def test_chat_remembers_the_conversation(client, in_memory):
    first = client.post("/chat", json={"message": "hello"}).json()
    thread_id = first["thread_id"]
    assert first["reply"] == "echo: hello (saw 1 messages)"

    second = client.post("/chat", json={"message": "again", "thread_id": thread_id}).json()
    assert second["reply"] == "echo: again (saw 3 messages)"  # user, assistant, user

    history = client.get(f"/history/{thread_id}").json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]

# def test_intentional_ci_failure():
#     assert False, "Intentional failure to demonstrate CI blocking deployment"