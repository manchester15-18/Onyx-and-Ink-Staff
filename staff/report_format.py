"""Normalize model-generated staff reports without changing their meaning."""
import re


STANDARD_SECTIONS = ('Summary', 'Work completed', 'Assumptions and risks', 'Next actions', 'Decisions needed')


def _standard_section(label):
    """Map common agent wording to the company report headings."""
    label=re.sub(r'[*_`#:]+','',str(label)).strip().lower()
    if re.search(r'^(summary|status|deliverable)$',label):return 'Summary'
    if re.search(r'(work completed|campaign copy|draft text|technical requirements|implementation|acceptance criteria|policy|dependency|decision register)',label):return 'Work completed'
    if re.search(r'(assumption|risk|constraint|blocker)',label):return 'Assumptions and risks'
    if re.search(r'(next action|next step|follow.?up)',label):return 'Next actions'
    if re.search(r'(open decision|decisions? needed|ceo approval|approval needed)',label):return 'Decisions needed'
    return None


def normalize_report(value):
    text=str(value).replace('\r\n','\n').replace('\r','\n').lstrip('\ufeff').strip()
    fenced=re.search(r'```(?:markdown|md)?\s*\n([\s\S]*?)\n```',text,re.I)
    prefix=text[:fenced.start()].strip() if fenced else ''
    if fenced and (not prefix or re.search(r'(?im)^(?:thought|action|final answer)\s*:',prefix)):
        text=fenced.group(1).strip()
    text=re.sub(r'(?im)^\s*(?:thought\s*:.*|action\s*:\s*final answer\s*|final answer\s*:)\s*$', '', text)
    text=re.sub(r'(?im)^\s*```(?:markdown|md)?\s*$', '', text)
    text=re.sub(r'(?m)^\s*```\s*$', '', text)
    # Provider safety labels are transport residue, not report content.
    text=re.sub(r'(?im)^\s*(?:user|response)\s+safety\s*:\s*(?:safe|unsafe)\s*$', '', text)
    text=re.sub(r'(?m)^\s*[*+-]\s{2,}', '- ', text)
    text=re.sub(r'[ \t]+$', '', text, flags=re.M)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def standardize_staff_report(title, value, agent='Staff'):
    """Apply the shared concise report layout while retaining the report's wording."""
    text=normalize_report(value)
    title=re.sub(r'\s+',' ',str(title or 'Staff update')).strip()[:160]
    agent=re.sub(r'\s+',' ',str(agent or 'Staff')).strip()[:80]
    text=re.sub(r'(?m)^#\s+.*\n*','',text,1).strip()
    sections={name:[] for name in STANDARD_SECTIONS};current='Summary'
    for line in text.splitlines():
        if re.match(r'^\s*prepared by\s*:',line,re.I):
            continue
        if re.fullmatch(r'\s*(?:\*{1,3}|_{1,3})\s*',line):
            continue
        match=re.match(r'^\s*(?:#{1,6}\s+|\*{0,2})([^:*#][^:]*?)(?:\*{0,2}):\s*(.*)$',line)
        heading=re.match(r'^\s*#{1,6}\s+(.+?)\s*$',line)
        label=(match.group(1) if match else heading.group(1) if heading else '')
        mapped=_standard_section(label)
        if mapped:
            current=mapped
            remainder=re.sub(r'^\*+|\*+$','',(match.group(2) if match else '')).strip()
            if remainder:sections[current].append(remainder)
            continue
        sections[current].append(re.sub(r'^\*+|\*+$','',line).rstrip())
    result=['# '+title,'','Prepared by: '+agent,'']
    for section in STANDARD_SECTIONS:
        lines=sections[section]
        meaningful=[line for line in lines if line.strip().lower() not in ('- none recorded.','none recorded.')]
        content='\n'.join(meaningful).strip()
        result.extend(['## '+section,'',content or '- None recorded.',''])
    return '\n'.join(result).rstrip()
