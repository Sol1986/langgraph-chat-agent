"""FastAPI around the LangGraph chatbot.

Postgres: conversation memory (LangGraph checkpoints), so chats survive restarts.
Redis:    per-conversation rate limit + a total message counter.
"""

import logging
import os
import re
import uuid
from functools import lru_cache

import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres import PostgresSaver
from pydantic import BaseModel

from app import deps
from app.graph import build_graph

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("chatbot")

MAX_MESSAGE_CHARS = 2000
THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ChatRequest(BaseModel):
    message: str = ""
    thread_id: str | None = None


@lru_cache(maxsize=1)
def get_graph():
    """Build the graph once per process, with Postgres as its memory."""
    checkpointer = PostgresSaver(deps.pg_pool())
    checkpointer.setup()  # creates LangGraph's tables if missing; safe on every start
    return build_graph(checkpointer)


def rate_limited(thread_id: str) -> bool:
    """Allow RATE_LIMIT_PER_MIN messages per conversation per minute (Redis counter)."""
    limit = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))
    key = f"rate:{thread_id}"
    try:
        r = deps.redis_client()
        count = r.incr(key)
        if count == 1:
            r.expire(key, 60)
        return count > limit
    except redis.RedisError:
        # Fail open: a Redis outage shouldn't take the chatbot down with it.
        log.warning("Redis unavailable, skipping rate limit")
        return False


def count_message() -> None:
    try:
        deps.redis_client().incr("stats:messages")
    except redis.RedisError:
        log.warning("Redis unavailable, message not counted")


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        # Liveness: the process is up. Never touches Postgres, Redis or OpenAI.
        # This is what the ALB and ECS health checks call.
        if os.getenv("FORCE_UNHEALTHY") == "1":  # switch for the rollback drill
            return JSONResponse(content={"status": "unhealthy"}, status_code=503)
        return {"status": "healthy"}

    @app.get("/health/ready")
    def ready():
        # Readiness: can we reach both data stores? For humans and monitoring.
        checks = {}
        try:
            with deps.pg_pool().connection(timeout=3) as conn:
                conn.execute("SELECT 1")
            checks["postgres"] = "ok"
        except Exception:
            log.exception("Postgres check failed")
            checks["postgres"] = "unreachable"
        try:
            deps.redis_client().ping()
            checks["redis"] = "ok"
        except Exception:
            log.exception("Redis check failed")
            checks["redis"] = "unreachable"
        healthy = all(v == "ok" for v in checks.values())
        status = "healthy" if healthy else "unavailable"
        return JSONResponse(content={"status": status, **checks}, status_code=200 if healthy else 503)

    @app.get("/")
    def index():
        # APP_VERSION is baked into the image as the git SHA at build time.
        return {
            "app": "langgraph-chatbot",
            "message": "Hello from version A",
            "version": os.getenv("APP_VERSION", "dev"),
        }

    @app.post("/chat")
    def chat(body: ChatRequest):
        message = body.message.strip()
        if not message:
            return JSONResponse(content={"error": "message is required"}, status_code=400)
        if len(message) > MAX_MESSAGE_CHARS:
            return JSONResponse(
                content={"error": f"message is longer than {MAX_MESSAGE_CHARS} characters"},
                status_code=400,
            )

        thread_id = body.thread_id or uuid.uuid4().hex
        if not THREAD_ID.match(thread_id):
            return JSONResponse(
                content={"error": "thread_id must be 1-64 letters, digits, - or _"},
                status_code=400,
            )

        if rate_limited(thread_id):
            return JSONResponse(content={"error": "Too many messages, wait a minute"}, status_code=429)

        config = {"configurable": {"thread_id": thread_id}}
        try:
            result = get_graph().invoke(
                {"messages": [{"role": "user", "content": message}]}, config
            )
        except Exception:
            log.exception("Chat failed")
            return JSONResponse(
                content={"error": "The assistant is unavailable right now"}, status_code=502
            )

        count_message()
        return {"thread_id": thread_id, "reply": result["messages"][-1]["content"]}

    @app.get("/history/{thread_id}")
    def history(thread_id: str):
        if not THREAD_ID.match(thread_id):
            return JSONResponse(content={"error": "invalid thread_id"}, status_code=400)
        state = get_graph().get_state({"configurable": {"thread_id": thread_id}})
        return {"thread_id": thread_id, "messages": state.values.get("messages", [])}

    @app.get("/stats")
    def stats():
        try:
            total = int(deps.redis_client().get("stats:messages") or 0)
        except redis.RedisError:
            return JSONResponse(content={"error": "Redis unavailable"}, status_code=503)
        return {"total_messages": total}

    return app


app = create_app()
