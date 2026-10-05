"""Per-agent model assignments across free-tier OpenAI-compatible providers.

Each agent uses its primary provider when credentials exist and falls back down its
chain (ending at Groq), so a missing key never stops the staff.
"""
import re

from groq_llm import GROQ_BASE_URL, GroqLLM, shared_budget

PROVIDERS = {
    'groq': {'label': 'Groq', 'key': 'GROQ_API_KEY', 'base_url': GROQ_BASE_URL,
             'model': 'qwen/qwen3.8-27b', 'rpm': 25, 'tpm': 8000, 'max_tokens': 1000},
    'gemini': {'label': 'Google Gemini', 'key': 'GEMINI_API_KEY',
               'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai/',
               'model': 'gemini-2.5-pro', 'rpm': 5, 'tpm': 250000, 'max_tokens': 4000},
    'cerebras': {'label': 'Cerebras', 'key': 'CEREBRAS_API_KEY', 'base_url': 'https://api.cerebras.ai/v1',
                 'model': 'gpt-oss-120b', 'rpm': 30, 'tpm': 60000, 'max_tokens': 2000},
    'cloudflare': {'label': 'Cloudflare Workers AI', 'key': 'CLOUDFLARE_API_TOKEN',
                   'base_url': 'https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1',
                   'model': '@cf/mistralai/mistral-small-3.1-24b-instruct', 'rpm': 60, 'tpm': 60000, 'max_tokens': 1500},
    'mistral': {'label': 'Mistral Codestral', 'key': 'CODESTRAL_API_KEY', 'base_url': 'https://codestral.mistral.ai/v1',
                'model': 'codestral-latest', 'rpm': 30, 'tpm': 60000, 'max_tokens': 1500},
}

ASSIGNMENTS = {
    'Cameron': ('gemini', 'groq'),
    'Avery': ('cerebras', 'groq'),
    'Morgan': ('groq',),
    'Jordan': ('cloudflare', 'mistral', 'groq'),
}


def _bounded(cfg, name, default, low, high):
    try:
        return max(low, min(high, int(cfg.get(name) or default)))
    except ValueError:
        return default


def provider_ready(name, cfg):
    if not (cfg.get(PROVIDERS[name]['key']) or '').strip():
        return False
    if name == 'cloudflare':
        # Same Free-plan confirmation the image tools require, so Workers AI never bills.
        return cfg.get('CLOUDFLARE_FREE_PLAN_CONFIRMED') == 'true' and bool(
            re.fullmatch('[a-fA-F0-9]{32}', cfg.get('CLOUDFLARE_ACCOUNT_ID') or ''))
    return True


def chain_for(agent, cfg):
    override = (cfg.get(agent.upper() + '_PROVIDER') or '').strip().lower()
    chain = ASSIGNMENTS.get(agent, ('groq',))
    if override in PROVIDERS:
        chain = (override,) + tuple(p for p in chain if p != override)
    return chain


def provider_for(agent, cfg):
    chain = chain_for(agent, cfg)
    for name in chain:
        if provider_ready(name, cfg):
            return name, name == chain[0]
    raise ValueError(f'No AI provider is configured for {agent}. Add GROQ_API_KEY (or the {PROVIDERS[chain[0]]["key"]} for its primary model) to .env.')


def build_llm(agent, cfg, max_tokens=None, timeout=60, max_retries=1):
    name, primary = provider_for(agent, cfg)
    spec = PROVIDERS[name]
    prefix = name.upper()
    # An agent-specific model only applies to its primary provider, never to a fallback.
    model = (primary and cfg.get(agent.upper() + '_MODEL')) or cfg.get(prefix + '_MODEL') or spec['model']
    rpm = _bounded(cfg, prefix + '_RPM', spec['rpm'], 1, 1000)
    tpm = _bounded(cfg, prefix + '_TPM', spec['tpm'], 2000, 2000000)
    tokens = _bounded(cfg, prefix + '_MAX_COMPLETION_TOKENS', spec['max_tokens'], 300, 8000)
    if max_tokens:
        # Gemini thinking tokens count against the cap; keep room for a visible answer.
        tokens = max(max_tokens, 2048) if name == 'gemini' else min(tokens, max_tokens)
    base_url = spec['base_url'].format(account=cfg.get('CLOUDFLARE_ACCOUNT_ID', ''))
    return GroqLLM(cfg[spec['key']].strip(), model=model, rpm=rpm, tpm=tpm, max_tokens=tokens, timeout=timeout,
                   max_retries=max_retries, base_url=base_url, provider=name, label=spec['label'],
                   budget=shared_budget(name, rpm, tpm))


def build_staff_llms(cfg, names=tuple(ASSIGNMENTS), **kwargs):
    return {name: build_llm(name, cfg, **kwargs) for name in names}


def describe(llms):
    return ', '.join(f'{name}: {llm.service_label} ({llm.model})' for name, llm in llms.items())
