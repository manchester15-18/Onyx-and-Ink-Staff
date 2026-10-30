"""Persistent staff assignments managed by the dashboard operating hours."""
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from staff.report_projects import active_project
try:
    from staff.staff_handoff import HandoffChain, REPORT_FILES
except ImportError:  # tests sometimes load this module top-level
    from staff_handoff import HandoffChain, REPORT_FILES

DEFAULT_OBJECTIVE='''Work only from the active project overview, consolidated report, current role reports, and assigned staff tasks. Treat approved facts as binding; treat assumptions, placeholders, and open decisions as unapproved. Work in dependency order: Morgan maintains one decision/dependency register and assigns the next concrete task; Jordan converts verified product and technical facts into tested Preview-ready work; Avery refines one current customer-facing copy or asset draft from verified facts; Cameron completes one current policy or compliance section with placeholders where a CEO decision is still needed. Update the existing role report or project document instead of creating duplicates. Record evidence, the next owner, and the remaining blocker. Do not publish to production, invent pricing, inventory, delivery promises, policy positions, or campaign claims. If a CEO decision truly blocks further useful work, Morgan sends one concise decision packet with the options, recommendation, impact, and exact approval needed; otherwise continue the next unblocked task.'''
BUSINESS_TZ=ZoneInfo('America/New_York')


def report_context(root):
    """Use the active project's current reports, not stale work from other projects."""
    _,_,folder=active_project(root)
    candidates=[folder/'overview.md',folder/'consolidated-project-report.md']
    candidates.extend(sorted(path for path in folder.glob('*.md') if path.name not in ('overview.md','consolidated-project-report.md')))
    candidates.extend(sorted((folder/'agent-reports').glob('*.md')) if (folder/'agent-reports').exists() else [])
    candidates.extend(sorted((folder/'manual').glob('**/*.md')) if (folder/'manual').exists() else [])
    sections=[]
    for path in candidates:
        try:text=path.read_text().strip()
        except OSError:continue
        if text:
            sections.append(path.stem.replace('_',' ').title()+':\n'+text[:650])
    try:
        try:from staff.tool_tasks import tool_results_context
        except ImportError:from tool_tasks import tool_results_context
        tool_section=tool_results_context(root)
    except Exception:tool_section=''
    if tool_section:sections.append(tool_section)
    return '\n\n'.join(sections)[:3200]


def valid_time(value):
    return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value or ''))


def in_window(start,stop,now=None):
    if not start and not stop:return True
    if not valid_time(start) or not valid_time(stop):return False
    current=(now or datetime.now(BUSINESS_TZ)).strftime('%H:%M')
    return start<=current<stop if start<stop else current>=start or current<stop


def run_active(root):
    path=Path(root)/'work'/'staff-run.lock'
    if not path.exists():return False
    with path.open('a') as stream:
        try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        fcntl.flock(stream,fcntl.LOCK_UN)
    return False


def provider_ready_epoch(root):
    try:return float(json.loads((Path(root)/'work'/'groq-cooldown.json').read_text()).get('readyEpoch') or 0)
    except (OSError,ValueError,TypeError):return 0


def project_brief_ready(root):
    """Autonomous work needs an approved project brief, not an empty template."""
    try:
        _,_,folder=active_project(root)
        text=(folder/'overview.md').read_text(encoding='utf-8').strip()
    except (OSError,ValueError):
        return False
    placeholders=('Add the business context','Add the outcomes this project must achieve','Define what completed, acceptable work looks like')
    return len(text)>=180 and not any(item in text for item in placeholders)

class StaffAutonomy:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'work'/'staff-autonomy.json';self.process=None

    def work_signature(self, state):
        """Identify meaningful new work without treating regenerated reports as new input."""
        project_id,_,folder=active_project(self.root)
        try:overview=(folder/'overview.md').read_text(encoding='utf-8')
        except OSError:overview=''
        try:
            from staff.staff_tasks import status as task_status
            tasks=[{'id':item.get('id'),'to':item.get('to'),'text':item.get('text'),'updated':item.get('updated')}
                   for item in task_status(self.root,100)
                   if item.get('project')==project_id and item.get('status') in ('pending','in_progress')]
        except (OSError,ValueError,TypeError):tasks=[]
        source=json.dumps({'project':project_id,'overview':overview,'objective':state.get('objective',''),'tasks':tasks},sort_keys=True)
        return hashlib.sha256(source.encode()).hexdigest()

    def load(self):
        base={'enabled':False,'start':'','stop':'','objective':DEFAULT_OBJECTIVE,'lastStarted':'','lastFinished':'','lastExit':None,'runNowPending':False,'capacityReadyEpoch':0,'consecutiveFailures':0,'lastFailure':'','handoff':{}}
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

    def record_unavailable(self, assignment_id, detail):
        """Never leave the dashboard claiming staff are working after a failed launch."""
        path=self.root/'work'/'staff-work-status.json'
        payload={'assignment':assignment_id,'status':'waiting','detail':detail,
                 'agents':{name:'waiting' for name in ('Morgan','Avery','Jordan','Cameron')},
                 'updated':datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')}
        try:
            existing=json.loads(path.read_text())
            if isinstance(existing,dict) and 'handoff' in existing:payload['handoff']=existing['handoff']
        except (OSError,ValueError,TypeError):pass
        path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(payload,indent=2));temp.chmod(0o600);temp.replace(path)

    def _write_work_status_with_handoff(self, handoff, status, detail):
        """Observable chain stage: the work-status file carries the handoff
        record so a reader can tell where a cycle is without reading code."""
        path=self.root/'work'/'staff-work-status.json'
        payload={'assignment':handoff.get('assignment_id',''),'status':status,'detail':detail,
                 'agents':{name:'waiting' for name in ('Morgan','Avery','Jordan','Cameron')},
                 'handoff':handoff,
                 'updated':datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')}
        path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(payload,indent=2));temp.chmod(0o600);temp.replace(path)

    def _derive_assignment(self):
        """Morgan's next concrete task, automatically, not on instruction.

        First a queued staff task for the active project, if one exists.
        Otherwise the first Next-actions item from the consolidated
        report; if even that is absent, Morgan's register-order task —
        deliberately generic, never an invented business item, because
        the specific item then comes from the overview/consolidated
        report already carried in the cycle directive."""
        project_id,label,folder=active_project(self.root)
        try:
            from staff.staff_tasks import status as task_status
            for item in task_status(self.root,100):
                if item.get('project')==project_id and item.get('status') in ('pending','in_progress'):
                    return str(item.get('to') or 'Morgan'),str(item.get('text') or '')
        except (OSError,ValueError,TypeError):pass
        try:
            lines=(folder/'consolidated-project-report.md').read_text(encoding='utf-8').splitlines()
            in_next=False
            for line in lines:
                lowered=line.lower()
                stripped=line.strip()
                if stripped.startswith('#'):
                    in_next='next action' in lowered
                    continue
                if 'next action' in lowered:
                    in_next=True
                    continue
                if in_next and stripped.startswith('- ') and len(stripped)>4:
                    task_text=stripped[2:].strip()
                    if task_text.rstrip('.').lower() in ('none recorded','none'):
                        continue
                    return 'Morgan',task_text
        except OSError:pass
        return 'Morgan',('Complete the next concrete unblocked deliverable for %s from its overview and '
                         'consolidated report, in Morgan\'s decision/dependency register order.' % label)

    def _qc_after_cycle(self, state):
        """Morgan QC-checks the just-delivered reports (automatic chain
        step). Returns the handoff record, or None if QC itself fails —
        in which case the healthy cycle is left exactly as before."""
        try:
            project_id,_,folder=active_project(self.root)
            agent_dir=folder/'agent-reports'
            reports={}
            for agent,filename in REPORT_FILES.items():
                try:reports[agent]=(agent_dir/filename).read_text(encoding='utf-8')
                except OSError:reports[agent]=''
            prev=state.get('handoff') or {}
            chain=HandoffChain(project_id)
            chain.retries=int(prev.get('retries') or 0)
            chain.corrections=list(prev.get('corrections') or [])
            chain.task=str(prev.get('task') or '')
            chain.owner=str(prev.get('owner') or 'Morgan')
            chain.assignment_id=str(prev.get('assignment_id') or '')
            stage=chain.run_qc(reports)
            handoff=chain.state()
            if stage=='qc_pass':
                self._write_work_status_with_handoff(handoff,'complete','All four agents delivered; Morgan QC passed.')
            elif stage=='decision_packet':
                packet=handoff.get('decision_packet') or {}
                self._write_work_status_with_handoff(handoff,'waiting','Decision packet: '+str(packet.get('approval_needed',''))[:220])
                self._deliver_decision_packet(handoff)
            else:
                self._write_work_status_with_handoff(handoff,'working','Morgan QC retry %d of %d: corrections sent for the next automatic delivery.' % (handoff['retries'],handoff['max_retries']))
            return handoff
        except Exception:
            return None

    def _deliver_decision_packet(self, handoff):
        """Email a decision packet to James exactly once.

        Oct 29, 2026 audit finding: a decision packet previously only
        landed in a JSON status file, so James never received an
        approval email for it. Email delivery must never break the
        autonomy loop, so every failure is swallowed and recorded.
        """
        try:
            if handoff.get('decision_packet_delivered'):
                return
            packet = handoff.get('decision_packet') or {}
            from staff.staff_email import StaffMail
            mail = StaffMail.from_env(self.root)
            project_id = handoff.get('project', '')
            label = project_id
            try:
                label = active_project(self.root)[1] or project_id
            except Exception:
                pass
            body_lines = [
                "Morgan -- CEO decision needed: " + str(label),
                "",
                "Assignment: " + str(handoff.get('assignment_id', '')),
                "",
                "Recommendation: " + str(packet.get('recommendation', '')),
                "",
                "Impact: " + str(packet.get('impact', '')),
                "",
                "Options:",
            ]
            for option in packet.get('options') or []:
                body_lines.append("- " + str(option))
            body_lines.extend([
                "",
                "Approval needed: " + str(packet.get('approval_needed', '')),
                "",
                "Reply with one of the options above, or tell Donald your "
                "decision, and the staff will proceed from there.",
            ])
            result = mail.deliver(
                'Morgan', ['Owner'],
                "CEO approval needed: " + str(label),
                "\n".join(body_lines), kind='message', important=False)
            record = {
                'assignment_id': handoff.get('assignment_id', ''),
                'project': project_id,
                'delivered': True,
                'result': str(result),
                'updated': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),
            }
            handoff['decision_packet_delivered'] = True
        except Exception as error:
            record = {
                'assignment_id': handoff.get('assignment_id', ''),
                'project': handoff.get('project', ''),
                'delivered': False,
                'result': 'delivery failed: %s' % type(error).__name__,
                'updated': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),
            }
            handoff['decision_packet_delivered'] = False
        try:
            out = self.root / 'work' / 'decision-packet-delivery.json'
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(record, indent=2))
        except Exception:
            pass

    def tick(self):
        state=self.load()
        if self.process and self.process.poll() is not None:
            if self.process.returncode==0:
                # Success pause (Oct 29, 2026: 900s -> 120s -> 60s, James
                # approved). Calculated from the routing budgets: the
                # cycle's 4 reservations land at cycle start, so cycle
                # duration + pause must beat ~70s to clear the trailing
                # 60s minute window. Observed cycles run 50-282s, so
                # 60s clears it every time (min 110s). Failure backoff,
                # per-model 300s cooldowns, and routing budgets stay.
                pause=60
                state['consecutiveFailures']=0;state['lastFailure']=''
                # Automatic handoff, step 3: Morgan QC-checks the delivery.
                # A QC retry is new input, so it must NOT be marked complete
                # (otherwise the signature guard below would never relaunch
                # with the corrections); a pass or decision packet stops
                # the chain exactly as a completed signature does.
                handoff_after=self._qc_after_cycle(state)
                if handoff_after is not None:
                    state['handoff']=handoff_after
                    if not handoff_after['stage'].startswith('qc_retry') and state.get('activeSignature'):
                        state['lastCompletedSignature']=state['activeSignature']
                elif state.get('activeSignature'):
                    state['lastCompletedSignature']=state['activeSignature']
                state.pop('activeSignature',None)
                try:(self.root/'work'/'groq-cooldown.json').unlink()
                except FileNotFoundError:pass
            else:
                failures=int(state.get('consecutiveFailures') or 0)+1
                # Brief retries recover transient network/DNS failures; sustained
                # outages back off to avoid pointless provider calls all afternoon.
                pause=max(min(900,120*(2**(failures-1))),provider_ready_epoch(self.root)-time.time())
                state['consecutiveFailures']=failures
                state['lastFailure']='Cloud providers did not answer; retrying automatically.'
                self.record_unavailable('',state['lastFailure'])
            state.update({'lastFinished':datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),'lastExit':self.process.returncode,'capacityReadyEpoch':time.time()+pause});self.process=None;self.save(state)
        provider_ready=provider_ready_epoch(self.root)
        if provider_ready>float(state.get('capacityReadyEpoch') or 0):
            state['capacityReadyEpoch']=provider_ready;self.save(state)
        forced=state.get('runNowPending') is True
        if not state['enabled'] or (not forced and not in_window(state['start'],state['stop'])) or run_active(self.root):return
        # Tool execution (Oct 30, 2026, James-approved): queued tool tasks
        # and TOOL bullets run deterministically here, before any LLM
        # cycle launches, so a dormant loop still executes approved work
        # and the next cycle reports the recorded results as supplied
        # evidence. Publishing actions without CEO approval are recorded
        # as skipped, never executed.
        try:
            try:from staff.tool_tasks import drain_tool_tasks
            except ImportError:from tool_tasks import drain_tool_tasks
            _project_id, _, _ = active_project(self.root)
            drain_tool_tasks(self.root, _project_id)
        except Exception:pass
        if not forced and time.time()<float(state.get('capacityReadyEpoch') or 0):return
        if not project_brief_ready(self.root):
            detail='Autonomous work is waiting for the active project overview to include background, goals, and desired outcomes. No model calls were made.'
            self.record_unavailable('',detail);return
        signature=self.work_signature(state)
        prev_handoff=state.get('handoff') or {}
        retry_pending=str(prev_handoff.get('stage','')).startswith('qc_retry')
        # Continuous mode (Oct 29, 2026): keep working after a qc_pass
        # while there is a concrete next action, instead of going
        # dormant on the signature guard. The generic fallback task is
        # NOT concrete work, so it never keeps the loop alive.
        continuous_run=False
        _project_id, _, _ = active_project(self.root)
        if (not forced and not retry_pending
                and state.get('continuous') is True
                and state.get('continuous_project') == _project_id
                and str(prev_handoff.get('stage', '')) == 'qc_pass'):
            _owner, _task = self._derive_assignment()
            if _task and not _task.startswith('Complete the next concrete unblocked deliverable'):
                if _task == state.get('continuous_last_task'):
                    if int(state.get('continuous_stall_count') or 0) + 1 >= 2:
                        state['continuous_stall_count'] = int(state.get('continuous_stall_count') or 0) + 1
                        self.record_unavailable('', 'Continuous work stalled: the next launch action has not changed across cycles. Awaiting a briefing change or CEO decision.')
                        self.save(state)
                        return
                continuous_run = True
        if not forced and not retry_pending and not continuous_run and state.get('lastCompletedSignature')==signature:
            if str(prev_handoff.get('stage',''))=='decision_packet':return
            self.record_unavailable('', 'Staff are ready. Autonomous work will resume when the project briefing changes or a new staff assignment arrives.')
            return
        log=self.root/'work'/'autonomous-staff.log';log.parent.mkdir(exist_ok=True)
        previous=report_context(self.root)
        directive=(state['objective']+'\n\nContinue working during the authorized operating hours. This is an active assignment, not a request to restate the plan. '
                   'Each agent must independently choose one concrete unfinished action in their permanent department, do that work, and record the result and next owner. '
                   'Do not wait for another agent or for an email unless a truly blocking decision is required.')
        if previous:
            directive+='\n\nUse this compact saved context for continuity. Do not repeat completed work or treat proposed figures as approved facts:\n\n'+previous
        if retry_pending and prev_handoff.get('corrections'):
            directive+='\n\nMorgan QC corrections from the previous delivery - address each one before any new work:\n'+'\n'.join('- '+item for item in prev_handoff['corrections'])
        directive=directive[:4000]
        env=dict(os.environ)
        import uuid
        assignment_id=datetime.now(BUSINESS_TZ).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        project_id, _, _ = active_project(self.root)
        # Automatic handoff, step 1: Morgan assigns the next concrete
        # task on every automatic launch, not only when instructed.
        # A QC retry keeps its retry count and corrections; a fresh
        # cycle resets them. This record is what the work-status
        # file and dashboard status surface as the chain stage.
        owner,task_text=self._derive_assignment()
        chain=HandoffChain(project_id)
        if retry_pending:
            chain.retries=int(prev_handoff.get('retries') or 0)
            chain.corrections=list(prev_handoff.get('corrections') or [])
            chain.decision_packet=None;chain.delivered={}
            chain.assign(task_text,owner,assignment_id,keep_retries=True)
        else:
            chain.assign(task_text,owner,assignment_id)
        with log.open('ab') as output:
            self.process=subprocess.Popen([sys.executable,'-u',str(self.root/'main.py'),'--assignment-id',assignment_id,'--project',project_id,'--directive',directive],cwd=self.root,stdout=output,stderr=output,env=env)
        state.update({'lastStarted':datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'),'lastExit':None,'runNowPending':False,'activeSignature':signature,'handoff':chain.state(),'continuous_last_task':task_text,'continuous_stall_count':(int(state.get('continuous_stall_count') or 0)+1 if task_text==state.get('continuous_last_task') else 0)});self.save(state)

    def stop(self):
        state=self.load();state['enabled']=False;self.save(state)
        if self.process and self.process.poll() is None:self.process.send_signal(signal.SIGINT)
        return state
