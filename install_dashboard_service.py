"""Start the persistent Windows Docker dashboard."""
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
if __name__=='__main__':
    if os.name!='nt':raise SystemExit('Run deploy/windows/start.ps1 from PowerShell on the Windows host.')
    result=subprocess.run(['powershell.exe','-NoProfile','-File',str(ROOT/'deploy'/'windows'/'start.ps1')],cwd=ROOT)
    if result.returncode:raise SystemExit(result.returncode)
    print('Enable Docker Desktop startup at Windows sign-in. Docker provides dashboard restart protection.')
