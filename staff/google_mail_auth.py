"""Local Google desktop authorization; no credentials are printed."""
import argparse
import json
import os
import uuid
from pathlib import Path
from dotenv import load_dotenv

SCOPES = ['https://mail.google.com/']

class GoogleMailAuth:
    def __init__(self, project_dir, user):
        self.directory = Path(project_dir)
        self.user = user
        self.token_path = self.directory/'work'/'google-mail-token.json'
        if not user or '@' not in user or '\n' in user or '\r' in user:
            raise ValueError('Set GOOGLE_MAIL_USER to the real Workspace login address.')

    def check(self):
        if not self.token_path.exists():
            raise ValueError('Run google_mail_auth.py to authorize Google mail first.')

    def save(self, credentials):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_name('google-mail-token-'+uuid.uuid4().hex+'.tmp')
        descriptor = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(credentials.to_json())
        temporary.replace(self.token_path)

    def token(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        self.check()
        try:
            credentials = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise ValueError('No refresh token.')
                credentials.refresh(Request())
                self.save(credentials)
            return credentials.token
        except Exception:
            raise RuntimeError('Google authorization could not be refreshed. Run google_mail_auth.py again; check Workspace policy.') from None

    def payload(self):
        return f'user={self.user}\x01auth=Bearer {self.token()}\x01\x01'

    def smtp_login(self, client):
        client.ehlo_or_helo_if_needed()
        payload = self.payload()
        client.auth('XOAUTH2', lambda challenge=None: payload if challenge is None else '')

    def imap_login(self, client):
        payload = self.payload().encode()
        client.authenticate('XOAUTH2', lambda challenge: payload if not challenge else b'')


def main():
    directory = Path(__file__).resolve().parent
    load_dotenv(directory/'.env')
    parser = argparse.ArgumentParser(description='Authorize the shared Google Workspace mailbox locally.')
    parser.add_argument('--client', default=str(directory/'google-oauth-client.json'))
    args = parser.parse_args()
    stage = 'reading the Desktop client file'
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import AuthorizedSession
        stage = 'checking GOOGLE_MAIL_USER in .env'
        if not os.getenv('GOOGLE_MAIL_USER', ''):
            print('Add GOOGLE_MAIL_USER=inbox@onyxandink.org to the project .env file.')
            return 1
        auth = GoogleMailAuth(directory, os.getenv('GOOGLE_MAIL_USER',''))
        stage = 'reading the Desktop client file'
        client_data = json.loads(Path(args.client).read_text())
        if 'installed' not in client_data:
            print('Use a Desktop app client JSON, rather than a Web client.')
            return 1
        flow = InstalledAppFlow.from_client_config(client_data, SCOPES)
        stage = 'browser sign-in and authorization'
        print('Sign in with the Workspace account that owns the staff aliases.')
        credentials = flow.run_local_server(host='localhost', port=0, open_browser=True,
            authorization_prompt_message='Opening Google sign-in in your browser.',
            success_message='Authorization received. You can close this tab.',
            login_hint=auth.user, prompt='consent', timeout_seconds=300)
        stage = 'checking the signed-in mailbox'
        with AuthorizedSession(credentials) as session:
            response = session.get('https://gmail.googleapis.com/gmail/v1/users/me/profile', timeout=30)
            if response.status_code != 200:
                reasons = set()
                try:
                    reasons = {item.get('reason','') for item in response.json().get('error',{}).get('errors',[])}
                except (ValueError, AttributeError, TypeError):
                    pass
                if reasons & {'accessNotConfigured','SERVICE_DISABLED'}:
                    print('Enable Gmail API in the Google Cloud project containing your OAuth client, then retry.')
                else:
                    print(f'Mailbox verification failed (HTTP {response.status_code}). Check Gmail API is enabled, Gmail is active for the account, and Workspace permits the app.')
                return 1
            if response.json().get('emailAddress','').lower() != auth.user.lower():
                print('The signed-in mailbox differs from GOOGLE_MAIL_USER. Sign in as the real account owning the aliases, or correct that setting.')
                return 1
        if not credentials.refresh_token:
            print('Google did not provide offline authorization. Retry setup and approve mail access.')
            return 1
        stage = 'saving local authorization'
        auth.save(credentials)
        print('Google mail authorized. No email sent. Keep the token file private.')
        return 0
    except ImportError:
        print('Install project requirements before Google setup.')
    except FileNotFoundError:
        print('Desktop client file missing. Save it as google-oauth-client.json in the project folder.')
    except json.JSONDecodeError:
        print('Desktop client file is not valid JSON. Download a fresh client JSON from Google Cloud.')
    except Exception as error:
        category = type(error).__name__
        if category == 'AccessDeniedError':
            print('Google access was denied. Check consent, test-user access, and Workspace app policy.')
        elif category == 'InvalidClientError':
            print('Google rejected the OAuth client. Download a fresh Desktop client JSON.')
        elif category == 'MismatchingStateError':
            print('The authorization callback did not match this setup attempt. Close old setup tabs and retry once.')
        else:
            print(f'Google setup failed during {stage} ({category}). No new authorization saved. Share this message and any Google error shown in the browser; omit passwords, tokens, and callback URLs.')
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
