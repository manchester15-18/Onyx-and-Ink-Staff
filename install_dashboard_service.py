"""Install the requested per-user macOS background dashboard."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
LABEL='org.onyxandink.staff-dashboard'
if __name__=='__main__':
    destination=Path.home()/'Library'/'LaunchAgents'/f'{LABEL}.plist'
    destination.parent.mkdir(parents=True,exist_ok=True)
    work=ROOT/'work';work.mkdir(exist_ok=True)
    settings={'Label':LABEL,'ProgramArguments':[sys.executable,'-u',str(ROOT/'dashboard.py')],
        'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,
        'StandardOutPath':str(work/'dashboard-service.log'),'StandardErrorPath':str(work/'dashboard-service-error.log'),
        'EnvironmentVariables':{'CREWAI_TRACING_ENABLED':'false','CREWAI_TELEMETRY_DISABLED':'true'}}
    destination.write_bytes(plistlib.dumps(settings));destination.chmod(0o600)
    domain=f'gui/{os.getuid()}'
    subprocess.run(['/bin/launchctl','bootout',domain+'/'+LABEL],capture_output=True)
    result=subprocess.run(['/bin/launchctl','bootstrap',domain,str(destination)],capture_output=True)
    if result.returncode:
        print('Background service installation failed. Check LaunchAgents permissions and the project location.');raise SystemExit(1)
    print('Dashboard installed to start at login and restart if it exits. Monitor on/off choice is preserved.')
