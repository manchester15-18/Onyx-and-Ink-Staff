"""Conservative GitHub code backup. Never exports runtime mail or credentials."""
from pathlib import Path
import re
import subprocess
import sys
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parent

def git(*args):
    result=subprocess.run(['git',*args],cwd=ROOT,capture_output=True)
    if result.returncode:raise RuntimeError('Git operation failed; check repository access locally.')
    return result.stdout

def update_changelog(changed,base_ref='uncommitted'):
    """Record every source update without copying code or private runtime contents."""
    digest=hashlib.sha256();digest.update(base_ref.encode())
    for name in changed:
        path=ROOT/name;digest.update(name.encode());digest.update(path.read_bytes() if path.exists() else b'deleted')
    marker='<!-- backup '+digest.hexdigest()+' -->'
    path=ROOT/'CHANGELOG.md';text=path.read_text() if path.exists() else '# Application changelog\n'
    if marker in text:return
    when=datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M %Z')
    descriptions={
        'dashboard/app.js':'Updated dashboard interactions and controls',
        'dashboard/index.html':'Updated dashboard pages and forms',
        'dashboard/style.css':'Updated dashboard appearance and responsive layout',
        'dashboard_mail.py':'Updated Gmail inbox access and message handling',
        'inbox_state.py':'Updated dashboard-only inbox cleanup and attachment uploads',
        'agent_chat.py':'Updated staff chat and Telegram conversations',
        'agent_actions.py':'Updated staff action capabilities and authorization checks',
        'staff_email.py':'Updated email preparation, attachments, and sending',
        'github_sync.py':'Updated GitHub backup and changelog safeguards',
        'Dockerfile':'Updated the production container definition',
        '.dockerignore':'Updated the container privacy boundary',
        '.github/workflows/deploy-oracle.yml':'Updated automatic Oracle deployment',
        'deploy/oracle/bootstrap.sh':'Updated Oracle host preparation',
        'deploy/oracle/deploy.sh':'Updated verified Oracle releases',
        'deploy/oracle/compose.yml':'Updated cloud services and persistent storage',
    }
    lines=[descriptions.get(name,'Updated '+name) for name in changed]
    entry='\n## '+when+' — Application update\n\n'+'\n'.join('- '+line+' (`'+name+'`).' for line,name in zip(lines,changed))+'\n\n'+marker+'\n'
    path.write_text(text.rstrip()+'\n'+entry)

def sync():
    if not (ROOT/'.git').exists():raise RuntimeError('Repository setup is not complete.')
    remote=git('remote','get-url','origin').decode().strip()
    if not re.fullmatch(r'(?:https://github\.com/|git@github\.com:)[\w.-]+/[\w.-]+(?:\.git)?',remote):raise RuntimeError('A GitHub origin without embedded credentials is required.')
    # Do not mix a user’s staged work with automated commits.
    if git('diff','--cached','--name-only').strip():raise RuntimeError('Staged changes need human review before automatic sync.')
    files=set(git('ls-files','-z').decode().split('\0'))|set(git('ls-files','--others','--exclude-standard','-z').decode().split('\0'))
    files.discard('')
    secrets=[value for key,value in dotenv_values(ROOT/'.env').items() if value and len(value)>=6 and any(word in key for word in ('KEY','TOKEN','PASSWORD','SECRET'))]
    patterns=[r'gsk_[A-Za-z0-9]{20,}',r'AIza[A-Za-z0-9_-]{25,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',r'gh[pousr]_[A-Za-z0-9]{20,}',r'github_pat_[A-Za-z0-9_]{20,}',r'\b\d{8,}:[A-Za-z0-9_-]{30,}']
    allowed=[]
    for name in sorted(files):
        path=ROOT/name
        deployment={'.dockerignore','Dockerfile','.github/workflows/deploy-oracle.yml','deploy/oracle/bootstrap.sh','deploy/oracle/deploy.sh','deploy/oracle/compose.yml'}
        safe=(name in ('.gitignore','.env.example','README.md','requirements.txt','CHANGELOG.md') or name in deployment or '/' not in name and name.endswith('.py') or name.startswith('tests/test_') and name.endswith('.py') or name.startswith('dashboard/') and name.count('/')==1 and path.suffix in ('.html','.css','.js'))
        if not safe or path.is_symlink():raise RuntimeError('An unexpected tracked file needs review; nothing uploaded.')
        if path.exists():
            text=path.read_text()
            if any(value in text for value in secrets) or any(re.search(pattern,text) for pattern in patterns):raise RuntimeError('Potential credentials detected; nothing uploaded.')
        allowed.append(name)
    # Fetch first; never force push, merge automatically, or overwrite remote changes.
    git('fetch','origin')
    branch=git('branch','--show-current').decode().strip()
    if branch!='main':raise RuntimeError('Automatic backups only run on main.')
    remote_ref=subprocess.run(['git','rev-parse','--verify','origin/main'],cwd=ROOT,capture_output=True)
    if remote_ref.returncode==0:
        ancestor=subprocess.run(['git','merge-base','--is-ancestor','origin/main','HEAD'],cwd=ROOT,capture_output=True)
        if ancestor.returncode:raise RuntimeError('GitHub has changes that need review before syncing.')
    changed=set(git('diff','--name-only').decode().splitlines())|set(git('ls-files','--others','--exclude-standard','-z').decode().strip('\0').split('\0'))
    changed=sorted(name for name in changed if name in allowed and name!='CHANGELOG.md')
    if changed:
        update_changelog(changed,git('rev-parse','HEAD').decode().strip())
        if 'CHANGELOG.md' not in allowed:allowed.append('CHANGELOG.md')
    if allowed:git('add','-A','--',*allowed)
    if git('diff','--cached','--name-only').strip():git('commit','-m','Back up Onyx and Ink application changes')
    git('push','-u','origin','main')
    print('GitHub code backup is up to date.')

if __name__=='__main__':
    try:sync()
    except Exception as error:
        print(str(error) if isinstance(error,RuntimeError) else 'Backup stopped; review local files. No secret details logged.',file=sys.stderr)
        sys.exit(1)
