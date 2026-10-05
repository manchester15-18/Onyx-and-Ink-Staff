"""Persistent staff assignments managed by the dashboard operating hours."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from report_projects import active_project

DEFAULT_OBJECTIVE='Continue advancing the current Onyx & Ink priorities. Start the highest-priority unfinished work immediately, make only verifiable progress, and ask the CEO one focused question only when a decision blocks the next useful action.'


def report_context(root):
    """Keep enough prior work for continuity without flooding every model call."""
    sections=[]
    for path in sorted((Path(root)/'reports').glob('**/*.md'), key=lambda item:item.stat().st_mtime, reverse=True)[:20]:
        try:text=path.read_text().strip()
        except OSError:continue
        if text:
            sections.append(path.stem.replace('_',' ').title()+':\n'+text[:450])
    return '\n\n'.join(sections)[:1800]


def valid_time(value):
    return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value or ''))


def in_window(start,stop,now=None):
    if not start and not stop:return True
    if not valid_time(start) or not valid_time(stop):return False
    current=(now or datetime.now()).strftime('%H:%M')
    return start<=current<stop if start<stop else current>=start or current<stop


def run_active(root):
    path=Path(root)/'work'/'staff-run.lock'
    if not path.exists():return False
    with path.open('a') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        fcntl.flock(stream,fcntl.LOCK_UN)
    return False


class StaffAutonomy:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'work'/'staff-autonomy.json';self.process=None

    def load(self):
        base={'enabled':False,'start':'','stop':'','objective':DEFAULT_OBJECTIVE,'lastStarted':'','lastFinished':'','lastExit':None,'runNowPending':False,'capacityReadyEpoch':0}
        try:base.update(json.loads(self.path.read_text()))
        except (OSError,ValueError,TypeError):pass
        for obsolete in ('interval','cycleNumber','lastFinishedEpoch'):
            base.pop(obsolete,None)
        return base

    def save(self,state):
        self.path.parent.mkdir(exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(state,indent=2));temp.chmod(0o600);temp.replace(self.path)

    def configure(self,data):
        state=self.load();start=str(data.get('start','')).strip();stop=str(data.get('stop','')).strip()
        if bool(start)!=bool(stop) or (start and (not valid_time(start) or not valid_time(stop) or start==stop)):raise ValueError('Choose both a start and stop time, or leave both blank.')
        objective=str(data.get('objective','')).strip()
        if not 10<=len(objective)<=2000:raise ValueError('Enter an objective between 10 and 2,000 characters.')
        state.update({'enabled':data.get('enabled') is True,'start':start,'stop':stop,'objective':objective})
        state.pop('interval',None);state.pop('lastFinishedEpoch',None)
        if data.get('runNow') is True:state['enabled']=True;state['runNowPending']=True
        self.save(state);return state

    def status(self):
        state=self.load();running=run_active(self.root)
        state.update({'running':running,'inWindow':in_window(state['start'],state['stop']),'capacityWaitSeconds':max(0,round(float(state.get('capacityReadyEpoch') or 0)-time.time()))})
        return state

    def tick(self):
        state=self.load()
        if self.process and self.process.poll() is not None:
            pause=61 if self.process.returncode==0 else 300
            state.update({'lastFinished':datetime.now().astimezone().isoformat(timespec='seconds'),'lastExit':self.process.returncode,'capacityReadyEpoch':time.time()+pause});self.process=None;self.save(state)
        forced=state.get('runNowPending') is True
        if not state['enabled'] or (not forced and not in_window(state['start'],state['stop'])) or run_active(self.root):return
        if not forced and time.time()<float(state.get('capacityReadyEpoch') or 0):return
        log=self.root/'work'/'autonomous-staff.log';log.parent.mkdir(exist_ok=True)
        previous=report_context(self.root)
        directive=(state['objective']+'\n\nContinue working during the authorized operating hours. This is an active assignment, not a request to restate the plan. '
                   'Each agent must independently choose one concrete unfinished action in their permanent department, do that work, and record the result and next owner. '
                   'Do not wait for another agent or for an email unless a truly blocking decision is required.')
        if previous:
            directive+='\n\nUse this compact saved context for continuity. Do not repeat completed work or treat proposed figures as approved facts:\n\n'+previous
        directive=directive[:4000]
        env=dict(os.environ)
        import uuid
        assignment_id=datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        with log.open('ab') as output:
            project_id, _, _ = active_project(self.root)
            self.process=subprocess.Popen([sys.executable,'-u',str(self.root/'main.py'),'--assignment-id',assignment_id,'--project',project_id,'--directive',directive],cwd=self.root,stdout=output,stderr=output,env=env)
        state.update({'lastStarted':datetime.now().astimezone().isoformat(timespec='seconds'),'lastExit':None,'runNowPending':False});self.save(state)

    def stop(self):
        state=self.load();state['enabled']=False;self.save(state)
        if self.process and self.process.poll() is None:self.process.send_signal(signal.SIGINT)
        return state
