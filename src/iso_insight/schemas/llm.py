"""LLM message contract."""

from __future__ import annotations

from pydantic import BaseModel


class LLMMessage(BaseModel):
    """One chat-completion message (role + plain-text content)."""

    model_config = {"frozen": True}

    role: str  # "system" | "user" | "assistant"
    content: str
