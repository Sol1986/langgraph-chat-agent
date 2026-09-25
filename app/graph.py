"""The LangGraph agent: one node that calls OpenAI.

    START -> chatbot -> END

The checkpointer (Postgres in the app, in-memory in tests) saves the message
history after every run, keyed by thread_id. That's what gives the bot memory.
"""

import operator
import os
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

SYSTEM_PROMPT = "You are a friendly, concise assistant. Keep answers under 120 words."


class ChatState(TypedDict):
    # operator.add appends new messages to the saved history instead of replacing it.
    messages: Annotated[list[dict], operator.add]


_client: OpenAI | None = None


def call_llm(messages: list[dict]) -> str:
    """The only function that talks to OpenAI. Tests replace it with a fake."""
    global _client
    if _client is None:
        _client = OpenAI(timeout=30)  # reads OPENAI_API_KEY from the environment
    response = _client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
    )
    return response.choices[0].message.content or ""


def chatbot(state: ChatState) -> dict:
    reply = call_llm(state["messages"])
    return {"messages": [{"role": "assistant", "content": reply}]}


def build_graph(checkpointer):
    builder = StateGraph(ChatState)
    builder.add_node("chatbot", chatbot)
    builder.add_edge(START, "chatbot")
    builder.add_edge("chatbot", END)
    return builder.compile(checkpointer=checkpointer)
