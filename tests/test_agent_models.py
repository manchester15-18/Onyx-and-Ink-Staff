import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('CREWAI_TELEMETRY_DISABLED', 'true')
os.environ.setdefault('OTEL_SDK_DISABLED', 'true')
import agent_models
from agent_models import build_llm, build_staff_llms, provider_for

FULL = {
    'GROQ_API_KEY': 'g', 'GEMINI_API_KEY': 'm', 'CEREBRAS_API_KEY': 'c', 'CODESTRAL_API_KEY': 'x',
    'CLOUDFLARE_API_TOKEN': 't', 'CLOUDFLARE_ACCOUNT_ID': 'a' * 32, 'CLOUDFLARE_FREE_PLAN_CONFIRMED': 'true',
}


class AgentModelTests(unittest.TestCase):
    def test_each_agent_uses_its_assigned_provider(self):
        llms = build_staff_llms(FULL)
        try:
            self.assertEqual({n: l.service for n, l in llms.items()},
                             {'Cameron': 'gemini', 'Avery': 'cerebras', 'Morgan': 'groq', 'Jordan': 'cloudflare'})
            self.assertEqual(llms['Cameron'].model, 'gemini-2.5-pro')
            self.assertEqual(llms['Avery'].model, 'gpt-oss-120b')
            self.assertIn('a' * 32, str(llms['Jordan'].client.base_url))
        finally:
            for llm in llms.values():
                llm.close()

    def test_missing_keys_fall_back_to_groq(self):
        cfg = {'GROQ_API_KEY': 'g', 'CLOUDFLARE_API_TOKEN': 't', 'CLOUDFLARE_ACCOUNT_ID': 'a' * 32}
        for agent in ('Cameron', 'Avery', 'Jordan'):
            self.assertEqual(provider_for(agent, cfg)[0], 'groq')
        self.assertEqual(provider_for('Jordan', {**cfg, 'CODESTRAL_API_KEY': 'x'})[0], 'mistral')
        with self.assertRaises(ValueError):
            provider_for('Morgan', {})

    def test_overrides_and_provider_specific_payloads(self):
        self.assertEqual(provider_for('Jordan', {**FULL, 'JORDAN_PROVIDER': 'mistral'})[0], 'mistral')
        gemini = build_llm('Cameron', {**FULL, 'CAMERON_MODEL': 'gemini-3-pro-preview'}, max_tokens=600)
        try:
            self.assertEqual(gemini.model, 'gemini-3-pro-preview')
            payload = gemini._prepare_completion_params([{'role': 'user', 'content': 'hi'}])
            self.assertNotIn('extra_body', payload)
            self.assertEqual(payload['reasoning_effort'], 'low')
            self.assertGreaterEqual(payload.get('max_tokens', 0), 2048)
        finally:
            gemini.close()
        fallback = build_llm('Cameron', {'GROQ_API_KEY': 'g', 'CAMERON_MODEL': 'gemini-3-pro-preview'})
        try:
            self.assertEqual(fallback.model, agent_models.PROVIDERS['groq']['model'])
        finally:
            fallback.close()


if __name__ == '__main__':
    unittest.main()
