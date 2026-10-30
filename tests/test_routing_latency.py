"""Latency / fail-fast tests for staff/model_routing.py (Oct 28, 2026).

No live provider calls: requests.post is mocked throughout. Covers:
  - a 30s per-request cap (was 45s);
  - a timed-out free model is cooled for the cycle and skipped by the
    next agent, instead of stalling every agent in turn;
  - a 404 (terminal) model is cooled for 24h and not retried next call;
  - an empty answer cools the model for the cycle;
  - the fallback chain itself is intact (a failed model still falls
    through to the next model in the same call).
"""
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from staff.model_routing import (
    REQUEST_TIMEOUT_SECONDS, SLOW_MODEL_COOLDOWN_SECONDS, RoutedLLM,
)


def _success(text="Answer"):
    response = Mock(ok=True)
    response.json.return_value = {"choices": [{"message": {"content": text}}]}
    return response


def _failure(status):
    return Mock(ok=False, status_code=status, headers={})


class RoutingLatencyTests(unittest.TestCase):
    def test_request_timeout_is_30_seconds(self):
        self.assertEqual(REQUEST_TIMEOUT_SECONDS, 30)
        with tempfile.TemporaryDirectory() as folder:
            llm = RoutedLLM(folder, {"OPENROUTER_API_KEY": "router-key"},
                            "Morgan", max_tokens=20)
            with patch("staff.model_routing.requests.post",
                       return_value=_success()) as post:
                llm.request("openrouter", [{"role": "user", "content": "Hi"}],
                            model="openrouter/free")
                self.assertEqual(post.call_args.kwargs["timeout"], 30)

    def test_timed_out_model_is_skipped_by_next_call(self):
        with tempfile.TemporaryDirectory() as folder:
            llm = RoutedLLM(
                folder,
                {"OPENROUTER_API_KEY": "router-key",
                 "OPENROUTER_MODELS": "slow/model:free,fast/model:free"},
                "Morgan", max_tokens=20)
            calls = [requests.Timeout("slow"), _success("Fast answer"),
                     _success("Fast answer 2")]
            with patch("staff.model_routing.requests.post",
                       side_effect=calls) as post:
                # Same call falls through to the next model.
                self.assertEqual(llm.call("Hello"), "Fast answer")
                self.assertTrue(llm.is_model_cooling_down("slow/model:free"))
                self.assertFalse(llm.is_model_cooling_down("fast/model:free"))
                # Next call goes straight to the fast model: 1 more post,
                # not another 30s stall on the slow model.
                self.assertEqual(llm.call("Hello again"), "Fast answer 2")
                self.assertEqual(post.call_count, 3)
                self.assertEqual(post.call_args.kwargs["json"]["model"],
                                 "fast/model:free")

    def test_404_model_is_not_retried_on_next_call(self):
        with tempfile.TemporaryDirectory() as folder:
            llm = RoutedLLM(
                folder,
                {"OPENROUTER_API_KEY": "router-key",
                 "OPENROUTER_MODELS": "gone/model:free,fast/model:free"},
                "Morgan", max_tokens=20)
            calls = [_failure(404), _success("Fast answer"),
                     _success("Fast answer 2")]
            with patch("staff.model_routing.requests.post",
                       side_effect=calls) as post:
                self.assertEqual(llm.call("Hello"), "Fast answer")
                self.assertTrue(llm.is_model_cooling_down("gone/model:free"))
                self.assertEqual(llm.call("Hello again"), "Fast answer 2")
                self.assertEqual(post.call_count, 3)

    def test_empty_answer_cools_model_for_the_cycle(self):
        with tempfile.TemporaryDirectory() as folder:
            llm = RoutedLLM(
                folder,
                {"OPENROUTER_API_KEY": "router-key",
                 "OPENROUTER_MODELS": "empty/model:free,fast/model:free"},
                "Morgan", max_tokens=20)
            empty = Mock(ok=True)
            empty.json.return_value = {"choices": [{"message": {"content": "  "}}]}
            calls = [empty, _success("Fast answer"), _success("Fast answer 2")]
            with patch("staff.model_routing.requests.post",
                       side_effect=calls) as post:
                self.assertEqual(llm.call("Hello"), "Fast answer")
                self.assertTrue(llm.is_model_cooling_down("empty/model:free"))
                self.assertEqual(llm.call("Hello again"), "Fast answer 2")
                self.assertEqual(post.call_count, 3)

    def test_slow_cooldown_is_cycle_scoped_not_terminal(self):
        # A timeout is transient: 300s, not the 24h terminal cooldown.
        self.assertEqual(SLOW_MODEL_COOLDOWN_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
