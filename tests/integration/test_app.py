"""Tests never call OpenAI: call_llm is replaced with a fake.

Unit tests run anywhere. The integration test needs real Postgres + Redis and
runs when DATABASE_URL and REDIS_URL are set (as they are in CI).
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app import graph as graph_module
from app import main


def fake_llm(messages: list[dict]) -> str:
    # Echo the last message and how much history the bot could see.
    return f"echo: {messages[-1]['content']} (saw {len(messages)} messages)"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(graph_module, "call_llm", fake_llm)
    app = main.create_app()
    return TestClient(app)


# ---------- integration test (real Postgres + Redis) ----------

needs_services = pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and os.getenv("REDIS_URL")),
    reason="needs DATABASE_URL and REDIS_URL",
)


@needs_services
def test_ready_and_chat_with_real_services(client):
    assert client.get("/health/ready").json() == {
        "status": "healthy",
        "postgres": "ok",
        "redis": "ok",
    }

    thread_id = f"test-{uuid.uuid4().hex[:8]}"
    r = client.post("/chat", json={"message": "persist me", "thread_id": thread_id})
    assert r.status_code == 200

    history = client.get(f"/history/{thread_id}").json()["messages"]
    assert history[0] == {"role": "user", "content": "persist me"}
    assert client.get("/stats").json()["total_messages"] >= 1
