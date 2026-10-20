"""Durable, role-owned staff assignments created from staff messages."""
import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo('America/New_York')
STAFF = ('Morgan', 'Avery', 'Jordan', 'Cameron')


def _path(root):
    return Path(root) / 'work' / 'staff-task-queue.json'


def _load(root):
    try:
        data = json.loads(_path(root).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and isinstance(data.get('tasks'), list) else {'tasks': []}
    except (OSError, ValueError, TypeError):
        return {'tasks': []}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def _save(root, data):
    _write(_path(root), data)


def _wake_staff(root):
    path = Path(root) / 'work' / 'staff-autonomy.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return
    if state.get('enabled') is True:
        state['runNowPending'] = True
        _write(path, state)


def enqueue(root, sender, recipient, text, project):
    if recipient not in STAFF:
        raise ValueError('Choose a named staff recipient.')
    instruction = ' '.join(str(text or '').split())
    if len(instruction) < 4:
        raise ValueError('Staff assignments need a clear instruction.')
    data = _load(root)
    now = datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')
    task = {'id': uuid.uuid4().hex, 'project': str(project or ''), 'from': str(sender or 'Staff'),
            'to': recipient, 'text': instruction[:2000], 'status': 'pending',
            'created': now, 'updated': now}
    data['tasks'].append(task)
    _save(root, data)
    _wake_staff(root)
    return task


def claim(root, recipient, project):
    data = _load(root)
    now = datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')
    for task in data['tasks']:
        if task.get('to') == recipient and task.get('status') == 'pending' and task.get('project') == str(project or ''):
            task.update({'status': 'in_progress', 'claimed': now, 'updated': now})
            _save(root, data)
            return task
    return None


def complete(root, task_id, summary):
    if not task_id:
        return
    data = _load(root)
    for task in data['tasks']:
        if task.get('id') == task_id:
            task.update({'status': 'complete', 'updated': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),
                         'result': ' '.join(str(summary or '').split())[:500]})
            _save(root, data)
            return


def release(root, task_id):
    if not task_id:
        return
    data = _load(root)
    for task in data['tasks']:
        if task.get('id') == task_id and task.get('status') == 'in_progress':
            task.update({'status': 'pending', 'updated': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')})
            _save(root, data)
            return


def status(root, limit=20):
    tasks = sorted(_load(root)['tasks'], key=lambda task: task.get('updated', ''), reverse=True)
    return tasks[:limit]
