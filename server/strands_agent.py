"""
strands_agent.py
~~~~~~~~~~~~~~~~
Strands SDK agent wrapper used by the FastAPI server.

This module encapsulates all Strands-specific concerns:
- Provider/model configuration (Bedrock or OpenAI) via environment variables.
- Tool registration (calculator, current_time, python_repl, use_aws).
- A streaming generator that yields human-visible text chunks (for WS/SSE/NDJSON).
- A single retry on AWS token expiry (ExpiredTokenException) to improve resilience.

Environment variables:
- STRANDS_PROVIDER: "bedrock" (default) or "openai"
- MODEL_ID: e.g. "us.anthropic.claude-sonnet-4-20250514-v1:0" (Bedrock)
             or "gpt-4o-mini" (OpenAI)
- AWS_REGION: e.g. "us-east-1" (when STRANDS_PROVIDER=bedrock)
- OPENAI_API_KEY: required when STRANDS_PROVIDER=openai
- STRANDS_TEMPERATURE: float, optional (default 0.5)

Usage:
    from strands_agent import StrandsAgentEngine
    engine = StrandsAgentEngine()
    async for text in engine.generate_stream(chat_history, cancel_event):
        ...
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator, Dict, Iterable, List, Optional, TypedDict

from botocore.exceptions import ClientError

# Strands core + tools
from strands import Agent
from strands_tools import calculator, current_time, python_repl, use_aws


class ChatMessageLike(TypedDict):
    """Minimal shape expected from server-side ChatMessage for history."""
    id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float


class StrandsAgentEngine:
    """
    Wraps a Strands Agent and exposes an async generator that yields displayable text chunks.

    Key responsibilities:
    - Build (and rebuild) the model/agent from environment config.
    - Stream agent events and forward only the visible text chunks (`event["data"]`).
    - Retry once on AWS credential expiry (ExpiredToken/ExpiredTokenException).

    Public API:
        generate_stream(messages, cancel) -> AsyncGenerator[str, None]
    """

    def __init__(self) -> None:
        self._build_agent()

    # ---------- configuration helpers ----------

    def _provider(self) -> str:
        """Return the selected provider: 'bedrock' (default) or 'openai'."""
        return (os.getenv("STRANDS_PROVIDER") or "bedrock").lower().strip()

    def _model_id(self) -> str:
        """Return the model ID, with sensible defaults per provider."""
        default = (
            "us.anthropic.claude-sonnet-4-20250514-v1:0"
            if self._provider() == "bedrock"
            else "gpt-4o-mini"
        )
        return os.getenv("MODEL_ID", default)

    def _temperature(self) -> float:
        try:
            return float(os.getenv("STRANDS_TEMPERATURE", "0.5"))
        except ValueError:
            return 0.5

    def _build_agent(self) -> None:
        """
        (Re)construct the underlying Strands Model and Agent.

        Called on initialization and on retry-able failures (e.g., AWS token expiry).
        """
        provider = self._provider()
        model_id = self._model_id()
        temperature = self._temperature()

        if provider == "openai":
            # OpenAI provider (requires OPENAI_API_KEY)
            from strands.models.openai import OpenAIModel

            self.model = OpenAIModel(
                client_args={"api_key": os.getenv("OPENAI_API_KEY")},
                model_id=model_id,
                params={"temperature": temperature},
            )
        else:
            # Bedrock provider (default) — requires valid AWS creds & access to the model in the chosen region
            from strands.models import BedrockModel

            self.model = BedrockModel(
                model_id=model_id,
                region_name=os.getenv("AWS_REGION", "us-east-1"),
                temperature=temperature,
            )

        # Register useful demo/dev tools; customize as needed
        self.agent = Agent(
            model=self.model,
            tools=[calculator, current_time, python_repl, use_aws],
            system_prompt=(
                "You are a professional, concise assistant. "
                "Prefer step-by-step clarity. When useful, call tools."
            ),
            callback_handler=None,  # important for streaming via iterators
        )

    # ---------- streaming helpers ----------

    async def _stream_once(self, prompt: str):
        """
        Start a single async stream from the agent and yield raw events.

        This surfaces the native Strands event dicts; the public generator maps
        them down to visible text chunks.
        """
        async for event in self.agent.stream_async(prompt):
            yield event

    async def generate_stream(
        self,
        messages: List[ChatMessageLike],
        cancel: asyncio.Event,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a response based on the provided chat history.

        Parameters
        ----------
        messages : list[ChatMessageLike]
            Prior conversation turns (system/user/assistant).
        cancel : asyncio.Event
            If set, the stream should stop promptly.

        Yields
        ------
        str
            Human-visible text deltas (ready to render as tokens in UI).
        """
        last_user = next((m["content"] for m in reversed(
            messages) if m["role"] == "user"), "")
        tried_refresh = False

        while True:
            try:
                async for event in self._stream_once(last_user):
                    if cancel.is_set():
                        return
                    # Strands emits visible text under "data"
                    data = event.get("data")
                    if data:
                        yield data
                return  # finished normally

            except ClientError as e:
                # Auto-recover once on expiring AWS creds (Bedrock)
                code = (e.response or {}).get("Error", {}).get("Code")
                if code in ("ExpiredToken", "ExpiredTokenException") and not tried_refresh:
                    tried_refresh = True
                    self._build_agent()
                    continue
                raise  # bubble other errors (or a second failure)

