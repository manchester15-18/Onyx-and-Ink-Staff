"""Conservative GitHub code backup. Never exports runtime mail or credentials."""
from pathlib import Path
import re
import subprocess
import sys
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parent

def git(*args):
    result=subprocess.run(['git',*args],cwd=ROOT,capture_output=True)
    if result.returncode:raise RuntimeError('Git operation failed; check repository access locally.')
    return result.stdout

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
        safe=(name in ('.gitignore','.env.example','README.md','requirements.txt') or '/' not in name and name.endswith('.py') or name.startswith('tests/test_') and name.endswith('.py') or name.startswith('dashboard/') and name.count('/')==1 and path.suffix in ('.html','.css','.js'))
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
    if allowed:git('add','-A','--',*allowed)
    if git('diff','--cached','--name-only').strip():git('commit','-m','Back up Onyx and Ink application changes')
    git('push','-u','origin','main')
    print('GitHub code backup is up to date.')

if __name__=='__main__':
    try:sync()
    except Exception as error:
        print(str(error) if isinstance(error,RuntimeError) else 'Backup stopped; review local files. No secret details logged.',file=sys.stderr)
        sys.exit(1)
