"""Persistent, scheduled staff work cycles managed by the dashboard."""
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

DEFAULT_OBJECTIVE='Continue advancing the current Onyx & Ink priorities. Start the highest-priority unfinished work immediately, make only verifiable progress, and ask the CEO one focused question only when a decision blocks the next useful action.'


def report_context(root):
    """Keep enough prior work for continuity without flooding every model call."""
    sections=[]
    for path in sorted((Path(root)/'reports').glob('*.md')):
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
        base={'enabled':False,'start':'','stop':'','interval':60,'objective':DEFAULT_OBJECTIVE,'lastStarted':'','lastFinished':'','lastExit':None,'lastFinishedEpoch':0,'runNowPending':False}
        try:base.update(json.loads(self.path.read_text()))
        except (OSError,ValueError,TypeError):pass
        return base

    def save(self,state):
        self.path.parent.mkdir(exist_ok=True);temp=self.path.with_suffix('.tmp');temp.write_text(json.dumps(state,indent=2));temp.chmod(0o600);temp.replace(self.path)

    def configure(self,data):
        state=self.load();start=str(data.get('start','')).strip();stop=str(data.get('stop','')).strip()
        if bool(start)!=bool(stop) or (start and (not valid_time(start) or not valid_time(stop) or start==stop)):raise ValueError('Choose both a start and stop time, or leave both blank.')
        try:interval=int(data.get('interval',60))
        except (TypeError,ValueError):raise ValueError('Cycle interval must be a whole number.') from None
        objective=str(data.get('objective','')).strip()
        if interval not in (60,120,240,480) or not 10<=len(objective)<=2000:raise ValueError('Choose a valid interval and enter an objective.')
        state.update({'enabled':data.get('enabled') is True,'start':start,'stop':stop,'interval':interval,'objective':objective})
        if data.get('runNow') is True:state['lastFinishedEpoch']=0;state['enabled']=True;state['runNowPending']=True
        self.save(state);return state

    def status(self):
        state=self.load();running=run_active(self.root)
        state.update({'running':running,'inWindow':in_window(state['start'],state['stop'])})
        return state

    def tick(self):
        state=self.load()
        if self.process and self.process.poll() is not None:
            state.update({'lastFinished':datetime.now().astimezone().isoformat(timespec='seconds'),'lastFinishedEpoch':time.time(),'lastExit':self.process.returncode});self.process=None;self.save(state)
        forced=state.get('runNowPending') is True
        if not state['enabled'] or (not forced and not in_window(state['start'],state['stop'])) or run_active(self.root):return
        if time.time()-float(state.get('lastFinishedEpoch') or 0)<int(state['interval'])*60:return
        log=self.root/'work'/'autonomous-staff.log';log.parent.mkdir(exist_ok=True)
        previous=report_context(self.root)
        directive=(state['objective']+'\n\nStart this work cycle now. This is an execution cycle, not a request to restate the plan. '
                   'Each department must choose one concrete unfinished action it can complete with its available tools, do that work, and record the result and next owner. '
                   'Do not wait for another agent or for an email unless a truly blocking decision is required.')
        if previous:
            directive+='\n\nUse this compact saved context for continuity. Do not repeat completed work or treat proposed figures as approved facts:\n\n'+previous
        directive=directive[:4000]
        env=dict(os.environ)
        with log.open('ab') as output:
            self.process=subprocess.Popen([sys.executable,'-u',str(self.root/'main.py'),'--directive',directive],cwd=self.root,stdout=output,stderr=output,env=env)
        state.update({'lastStarted':datetime.now().astimezone().isoformat(timespec='seconds'),'lastExit':None,'runNowPending':False});self.save(state)

    def stop(self):
        state=self.load();state['enabled']=False;self.save(state)
        if self.process and self.process.poll() is None:self.process.send_signal(signal.SIGINT)
        return state
