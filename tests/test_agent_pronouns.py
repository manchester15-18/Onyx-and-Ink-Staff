"""Pronoun hardwiring: agents use their own pronouns, never they/them.

James's directive (Oct 31, 2026): Morgan is she/her; Avery, Jordan,
and Cameron are he/him. (He listed Cameron twice and omitted Jordan;
the second Cameron is read as Jordan, matching the existing voice
profiles.) The clause lives in three places: the PRONOUNS map, every
VOICES persona entry, and the act() tool-loop prompt, plus the CrewAI
backstories in main.py. These tests pin all of them so a later voice
or persona edit cannot silently drop the pronouns again.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.agent_chat import PRONOUNS, VOICES, AgentChat


class PronounMapTests(unittest.TestCase):
    def test_pronoun_map(self):
        self.assertEqual(PRONOUNS, {"Morgan": "she/her", "Avery": "he/him",
                                    "Jordan": "he/him", "Cameron": "he/him"})

    def test_every_voice_persona_carries_its_pronoun_clause(self):
        for agent, pronoun in PRONOUNS.items():
            self.assertIn("Your pronouns are " + pronoun, VOICES[agent])
            self.assertIn("never they/them", VOICES[agent])


class ActPromptPronounTests(unittest.TestCase):
    def _prompt_text(self, agent):
        class FakeLLM:
            def __init__(self):
                self.calls = []

            def call(self, messages):
                self.calls.append([dict(m) for m in messages])
                return "Hello back."

        with tempfile.TemporaryDirectory() as tmp:
            chat = AgentChat(Path(tmp))
            fake = FakeLLM()
            messages = [{"role": "user", "content": "Hi"}]
            chat.act(agent, messages, "test-1", llm=fake, source="dashboard")
        return " ".join(str(m.get("content", "")) for m in fake.calls[0])

    def test_act_prompt_uses_she_her_for_morgan(self):
        self.assertIn("Your pronouns are she/her", self._prompt_text("Morgan"))

    def test_act_prompt_uses_he_him_for_jordan(self):
        prompt = self._prompt_text("Jordan")
        self.assertIn("Your pronouns are he/him", prompt)
        self.assertNotIn("she/her", prompt)


class BackstoryPronounTests(unittest.TestCase):
    def test_main_backstories_carry_pronoun_clauses(self):
        text = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        self.assertIn("Your name is Morgan.", text)
        self.assertIn("Your name is Morgan. Your pronouns are she/her", text)
        for agent in ("Avery", "Jordan", "Cameron"):
            self.assertIn("Your name is %s. Your pronouns are he/him" % agent, text)
