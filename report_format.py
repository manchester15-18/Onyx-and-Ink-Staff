"""Normalize model-generated staff reports without changing their meaning."""
import re


def normalize_report(value):
    text=str(value).replace('\r\n','\n').replace('\r','\n').lstrip('\ufeff').strip()
    fenced=re.search(r'```(?:markdown|md)?\s*\n([\s\S]*?)\n```',text,re.I)
    prefix=text[:fenced.start()].strip() if fenced else ''
    if fenced and (not prefix or re.search(r'(?im)^(?:thought|action|final answer)\s*:',prefix)):
        text=fenced.group(1).strip()
    text=re.sub(r'(?im)^\s*(?:thought\s*:.*|action\s*:\s*final answer\s*|final answer\s*:)\s*$', '', text)
    text=re.sub(r'(?im)^\s*```(?:markdown|md)?\s*$', '', text)
    text=re.sub(r'(?m)^\s*```\s*$', '', text)
    text=re.sub(r'(?m)^\s*[*+-]\s{2,}', '- ', text)
    text=re.sub(r'[ \t]+$', '', text, flags=re.M)
    return re.sub(r'\n{3,}', '\n\n', text).strip()
