"""Safe local project folders for staff reports."""
import json
import re
import shutil
import uuid
from pathlib import Path
from staff.report_format import normalize_report, standardize_staff_report

DEFAULT_PROJECT = "onboarding"
DEFAULT_LABEL = "Onyx & Ink Priorities"
OVERVIEW_FILE = 'overview.md'

def _overview_template(label):
    return f'''# {label} — Project Overview

## Background
Add the business context, customer need, and relevant constraints.

## Goals
- Add the outcomes this project must achieve.

## Desired outcomes
- Define what completed, acceptable work looks like.

## Scope and decisions
Record approved decisions, owners, and important exclusions.
'''


def project_slug(value):
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not text or len(text) > 64:
        raise ValueError("Enter a project name using 64 characters or fewer.")
    return text


def _state_path(root):
    return Path(root) / "work" / "report-projects.json"


def _load(root):
    fallback = {"active": DEFAULT_PROJECT, "labels": {DEFAULT_PROJECT: DEFAULT_LABEL}}
    try:
        saved = json.loads(_state_path(root).read_text())
        if isinstance(saved, dict):
            base={"active": project_slug(saved.get("active", DEFAULT_PROJECT)), "labels": {}}
            labels = saved.get("labels", {})
            if isinstance(labels, dict):
                for key, label in labels.items():
                    slug = project_slug(key)
                    if isinstance(label, str) and label.strip():
                        base["labels"][slug] = label.strip()[:80]
            return base
    except (OSError, ValueError, TypeError):
        pass
    return fallback


def _save(root, state):
    path = _state_path(root)
    path.parent.mkdir(exist_ok=True)
    # The dashboard and an autonomous run may touch project metadata together.
    # A unique replacement file prevents one writer from deleting another's temp file.
    temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def ensure_project(root, name=None, label=None, activate=False):
    state = _load(root)
    slug = project_slug(name or state["active"])
    display = str(label or state["labels"].get(slug) or str(name or slug).replace("-", " ").title()).strip()[:80]
    state["labels"][slug] = display
    if activate:
        state["active"] = slug
    folder = Path(root) / "reports" / slug
    folder.mkdir(parents=True, exist_ok=True)
    overview=folder/OVERVIEW_FILE
    if not overview.exists():_write_report(overview,_overview_template(display))
    _save(root, state)
    return slug, display, folder


def project_overview(root, project=None):
    slug,label,folder=ensure_project(root,project)
    path=folder/OVERVIEW_FILE
    try:body=path.read_text(encoding='utf-8')
    except OSError:body=_overview_template(label);_write_report(path,body)
    return {'project':slug,'project_name':label,'body':body[:20000]}


def save_project_overview(root, project, body):
    slug,label,folder=ensure_project(root,project)
    body=str(body or '').strip()
    if not body or len(body)>20000:raise ValueError('Enter project background, goals, and desired outcomes within 20,000 characters.')
    if not re.search(r'(?m)^#\s+',body):body='# '+label+' — Project Overview\n\n'+body
    path=folder/OVERVIEW_FILE
    _write_report(path,body)
    from staff.report_records import metadata_many
    metadata_many(root,[path])
    return {'project':slug,'project_name':label,'body':body}


def project_context(root, project=None, limit=6000):
    """Stable, bounded briefing injected into fresh conversations and tool results."""
    overview=project_overview(root,project);folder=Path(root)/'reports'/overview['project']
    sections=['Active project: '+overview['project_name'],overview['body'][:3000]]
    for path in sorted((folder/'agent-reports').glob('*.md')) if (folder/'agent-reports').exists() else []:
        sections.append(path.stem.replace('-',' ').title()+':\n'+path.read_text(encoding='utf-8')[:900])
    return '\n\n'.join(sections)[:limit]


def active_project(root):
    state = _load(root)
    return ensure_project(root, state["active"])


def projects(root):
    state = _load(root)
    report_root = Path(root) / "reports"
    report_root.mkdir(exist_ok=True)
    slugs = set(state["labels"])
    slugs.update(path.name for path in report_root.iterdir() if path.is_dir() and not path.name.startswith("."))
    active = state["active"]
    return [
        {"id": slug, "name": state["labels"].get(slug, slug.replace("-", " ").title()), "active": slug == active}
        for slug in sorted(slugs, key=lambda value: (value != active, state["labels"].get(value, value).lower()))
    ]


def _write_report(path, body):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');temporary.write_text(body.rstrip()+'\n');temporary.chmod(0o600);temporary.replace(path)


def create_report(root,project,agent,title,body):
    """Create or update the same named manual report for an agent and project."""
    if agent not in ('Morgan','Avery','Jordan','Cameron'):
        raise ValueError('Choose Morgan, Avery, Jordan, or Cameron.')
    title=str(title or '').strip();body=str(body or '').strip()
    if not title or len(title)>160 or not body or len(body)>50000:
        raise ValueError('Enter a title and report body within the allowed length.')
    slug,label,folder=ensure_project(root,project)
    path=folder/'manual'/agent.lower()/(project_slug(title)+'.md')
    if not re.search(r'(?m)^#\s+',body):body='# '+title+'\n\n'+body
    existed=path.exists();_write_report(path,body)
    from staff.report_records import metadata_many
    metadata_many(root,[path])
    return {'id':str(path.relative_to(Path(root)/'reports')),'project':slug,'project_name':label,'updated':existed}


def agent_assignment_path(root,project,filename):
    slug,label,folder=ensure_project(root,project)
    if filename not in ('morgan-operations.md','avery-marketing.md','jordan-web-it.md','cameron-hr-legal.md'):raise ValueError('Unknown agent report.')
    return slug,label,folder/'agent-reports'/filename


def consolidated_project_report(root,project=None):
    """Write one current project briefing from the overview and role reports."""
    slug,label,folder=ensure_project(root,project)
    overview=normalize_report(project_overview(root,slug)['body'])
    overview=re.sub(r'(?m)^#\s+.*\n*','',overview,1).strip()
    sections=['# '+label+' \u2014 Consolidated Project Report','','## Project overview','',overview]
    labels={'morgan-operations.md':'Morgan · Operations','avery-marketing.md':'Avery · Marketing','jordan-web-it.md':'Jordan · Web / IT','cameron-hr-legal.md':'Cameron · HR / Legal'}
    for filename,role in labels.items():
        path=folder/'agent-reports'/filename
        if path.exists():
            body=normalize_report(path.read_text(encoding='utf-8'))
            body=re.sub(r'(?m)^#\s+.*\n*','',body,1).strip()
            sections.extend(['','## '+role,'',body or 'No current update saved.'])
    target=folder/'consolidated-project-report.md';existing=target.exists()
    _write_report(target,'\n'.join(sections))
    from staff.report_records import metadata_many
    metadata_many(root,[target])
    return {'project':slug,'project_name':label,'id':str(target.relative_to(Path(root)/'reports')),'updated':existing,'path':target}


def clean_previous_reports(root, project=None):
    """Apply the company format to historic agent and manually created reports."""
    _, _, folder=ensure_project(root,project)
    changed=0
    for path in folder.rglob('*.md'):
        relative=path.relative_to(folder)
        if path.name in (OVERVIEW_FILE,'consolidated-project-report.md') or 'archive' in relative.parts:
            continue
        before=path.read_text(encoding='utf-8')
        owner=next((name for name in ('Morgan','Avery','Jordan','Cameron') if name.lower() in relative.parts or name.lower() in path.stem), 'Staff')
        after=standardize_staff_report(path.stem.replace('-',' ').title(),before,owner)
        if before.rstrip() != after:
            _write_report(path,after);changed+=1
    return changed


def repair_agent_report_tree(root, project):
    """Keep one visible agent-report folder and archive old accidental nesting."""
    slug,_,folder=ensure_project(root,project)
    current=folder/'agent-reports';nested=current/'agent-reports'
    current.mkdir(parents=True,exist_ok=True)
    archived=0;recovered=0
    if nested.is_dir():
        archive=folder/'archive'/'legacy-agent-reports'
        for path in sorted(nested.rglob('*.md')):
            canonical=current/path.name
            if canonical.exists():
                archive.mkdir(parents=True,exist_ok=True)
                target=archive/path.name
                if target.exists():target=archive/(path.stem+'-'+uuid.uuid4().hex[:8]+path.suffix)
                path.replace(target);archived+=1
            else:
                path.replace(canonical);recovered+=1
        for directory in sorted(nested.rglob('*'),key=lambda item:len(item.parts),reverse=True):
            if directory.is_dir():
                try:directory.rmdir()
                except OSError:pass
        try:nested.rmdir()
        except OSError:pass
    return {'project':slug,'archived':archived,'recovered':recovered}


def consolidate_assignment_reports(root,project):
    """Keep one current report per agent and move prior assignment snapshots to archive."""
    slug,label,folder=ensure_project(root,project);assignments=folder/'assignments';archive=folder/'archive'/'assignments';current=folder/'agent-reports'
    filenames=('morgan-operations.md','avery-marketing.md','jordan-web-it.md','cameron-hr-legal.md')
    repaired=repair_agent_report_tree(root,slug)
    latest={}
    if assignments.exists():
        for path in assignments.glob('*/*.md'):
            if path.name in filenames and (path.name not in latest or path.stat().st_mtime>latest[path.name].stat().st_mtime):latest[path.name]=path
    updated=0
    for name,path in latest.items():
        target=current/name
        if not target.exists() or path.stat().st_mtime>target.stat().st_mtime:
            _write_report(target,path.read_text());updated+=1
    moved=0
    if assignments.exists():
        archive.mkdir(parents=True,exist_ok=True)
        for directory in list(assignments.iterdir()):
            if not directory.is_dir():continue
            target=archive/directory.name
            if target.exists():target=archive/(directory.name+'-'+uuid.uuid4().hex[:8])
            directory.replace(target);moved+=1
        try:assignments.rmdir()
        except OSError:pass
    from staff.report_records import metadata_many
    metadata_many(root,[folder/OVERVIEW_FILE,*current.glob('*.md')])
    summary=consolidated_project_report(root,slug)
    return {'project':slug,'moved':moved,'updated':updated,'archived_legacy_reports':repaired['archived'],'recovered_legacy_reports':repaired['recovered'],'consolidated':summary['id']}

def delete_report(root,identifier):
    report_root=(Path(root)/'reports').resolve();path=(report_root/str(identifier)).resolve()
    if not path.is_relative_to(report_root) or path.suffix!='.md' or not path.is_file():
        raise ValueError('Choose a saved report.')
    project_root=(report_root/path.relative_to(report_root).parts[0]).resolve();path.unlink()
    parent=path.parent
    while parent!=project_root and parent.is_relative_to(project_root):
        try:parent.rmdir()
        except OSError:break
        parent=parent.parent


def delete_project(root,project):
    state=_load(root);slug=project_slug(project);report_root=(Path(root)/'reports').resolve();folder=(report_root/slug).resolve()
    if not folder.is_relative_to(report_root) or not folder.is_dir():raise ValueError('Choose a saved project.')
    shutil.rmtree(folder)
    state['labels'].pop(slug,None)
    remaining=sorted(path.name for path in report_root.iterdir() if path.is_dir() and not path.name.startswith('.'))
    if remaining:
        state['active']=remaining[0]
        state['labels'].setdefault(remaining[0],remaining[0].replace('-',' ').title())
        _save(root,state)
    else:
        state={'active':DEFAULT_PROJECT,'labels':{DEFAULT_PROJECT:DEFAULT_LABEL}};_save(root,state)
        (report_root/DEFAULT_PROJECT).mkdir(parents=True,exist_ok=True)
    return state['active']


def migrate_legacy_reports(root, project=DEFAULT_PROJECT):
    """Move the original flat report files into one project without overwriting."""
    _, _, destination = ensure_project(root, project, DEFAULT_LABEL, activate=True)
    for source in (Path(root) / "reports").glob("*.md"):
        target = destination / source.name
        if target.exists():
            target = destination / (source.stem + "-legacy" + source.suffix)
        source.replace(target)
    return destination
