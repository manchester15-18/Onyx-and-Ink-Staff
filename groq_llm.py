"""CrewAI integration with Groq's OpenAI-compatible chat endpoint."""
import json
import math
import threading
import time
from collections import deque

import httpx
from crewai.llms.providers.openai.completion import OpenAICompletion

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class RequestBudget:
    """Conservative per-process pacing; all agents and SDK retries share it."""

    def __init__(self, requests_per_minute=25, tokens_per_minute=7000):
        self.rpm = requests_per_minute
        self.tpm = tokens_per_minute
        self.entries = deque()
        self.lock = threading.Lock()

    def before_request(self, request):
        payload = json.loads(request.content)
        # Estimate input tokens with room for formatting; reserve the output cap too.
        # This is an estimate, not Groq's exact tokenizer or account usage meter.
        prompt = json.dumps({k: payload[k] for k in ("messages", "tools") if k in payload}, ensure_ascii=False)
        tokens = math.ceil(len(prompt) / 3) + 100 + payload.get("max_completion_tokens", 1500)
        if tokens > self.tpm:
            raise ValueError(
                "This request is too large for the configured Groq token budget. "
                "Shorten the directive/reports or set GROQ_TPM to your account's actual limit."
            )
        with self.lock:
            while True:
                now = time.monotonic()
                while self.entries and now - self.entries[0][0] >= 61:
                    self.entries.popleft()
                if len(self.entries) < self.rpm and sum(n for _, n in self.entries) + tokens <= self.tpm:
                    self.entries.append((now, tokens))
                    return
                delay = max(0.1, self.entries[0][0] + 61 - now)
                print(f"Groq free-tier pacing: waiting {delay:.0f}s for request/token capacity.", flush=True)
                time.sleep(min(delay, 60))


class GroqLLM(OpenAICompletion):
    """Use the installed native client, without LiteLLM or Gemini dependencies."""

    def __init__(self, api_key, model="openai/gpt-oss-120b", rpm=25, tpm=7000, max_tokens=1500, timeout=60, max_retries=3):
        model = model.removeprefix("groq/")
        self.budget = RequestBudget(rpm, tpm)
        http_client = httpx.Client(event_hooks={"request": [self.budget.before_request]})
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=GROQ_BASE_URL,
            temperature=0.3,
            max_completion_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            client_params={"http_client": http_client},
        )

    def _prepare_completion_params(self, messages, tools=None):
        # Preserve tool-call IDs and other CrewAI fields while normalizing text blocks.
        normalized = []
        for message in messages:
            item = dict(message)
            content = item.get("content")
            if isinstance(content, list):
                if any(not isinstance(block, dict) or block.get("type", "text") != "text" or not isinstance(block.get("text", ""), str) for block in content):
                    raise ValueError("This Groq model supports text content only.")
                item["content"] = "\n".join(block.get("text", "") for block in content)
            elif isinstance(content, dict):
                if not isinstance(content.get("text"), str):
                    raise ValueError("This Groq model supports text content only.")
                item["content"] = content["text"]
            normalized.append(item)
        messages = normalized
        params = super()._prepare_completion_params(messages, tools)
        # GPT-OSS uses its own completion boundaries; omit stop/prefill parameters.
        params.pop("stop", None)
        if self.model.startswith("openai/gpt-oss-"):
            params["reasoning_effort"] = "low"
            # CrewAI expects ReAct tool instructions in message.content. GPT-OSS can
            # otherwise return only Groq's separate reasoning field, which CrewAI
            # treats as an empty response before it can run the requested tool.
            extra_body = dict(params.get("extra_body") or {})
            extra_body["include_reasoning"] = False
            params["extra_body"] = extra_body
            if tools:
                params["parallel_tool_calls"] = False
        return params

    def supports_stop_words(self):
        return False

    def get_context_window_size(self):
        return 100000

    def close(self):
        self.client.close()
