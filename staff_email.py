"""Named staff mailboxes, internal-only mail tools, and autonomous report delivery."""
import os
import re
import smtplib
import ssl
import uuid
import fcntl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

STAFF = ('Morgan', 'Avery', 'Jordan', 'Cameron')


def valid_address(value):
    return bool(re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value)) and value.isascii()


class StaffMail:
    def __init__(self, project_dir, mode='off', addresses=None, host='', port=587,
                 security='starttls', credentials=None, limit=12, team_updates=True, bcc='', oauth=None):
        if mode not in ('off', 'draft', 'send'):
            raise ValueError('STAFF_EMAIL_MODE must be off, draft, or send.')
        self.oauth = oauth
        self.mode = mode
        self.addresses = addresses or {}
        self.host, self.port, self.security = host, port, security
        self.credentials = credentials or {}
        self.limit, self.count = limit, 0
        self.team_updates = team_updates
        self.bcc = bcc
        self.run_id = uuid.uuid4().hex
        self.outbox = Path(project_dir) / 'work' / 'email-outbox' / self.run_id
        self.completed = set()
        self.reports = {}
        self.reported = set()
        self.tool_count = 0
        if mode == 'off':
            return
        if bcc and not valid_address(bcc):
            raise ValueError('Set a valid OWNER_BCC_EMAIL address.')
        for name in (*STAFF, 'Owner'):
            if not valid_address(self.addresses.get(name, '')):
                raise ValueError(f'Set a valid {name.upper()}_EMAIL address before enabling staff email.')
        if len({self.addresses[n].lower() for n in STAFF}) != len(STAFF):
            raise ValueError('Each agent needs a distinct mailbox or authorized alias.')
        if mode == 'send':
            if not host or port not in (465, 587) or security not in ('ssl', 'starttls'):
                raise ValueError('Configure SMTP_HOST, SMTP_PORT (465 or 587), and SMTP_SECURITY (ssl or starttls).')
            if (security == 'ssl' and port != 465) or (security == 'starttls' and port != 587):
                raise ValueError('Use ssl with port 465, or starttls with port 587.')
            if oauth:
                oauth.check()
            for name in (() if oauth else STAFF):
                user, password = self.credentials.get(name, ('', ''))
                if not user or not password:
                    raise ValueError(f'Configure SMTP credentials for {name}; never paste passwords into chat.')

    @classmethod
    def from_env(cls, project_dir, mode=None):
        addresses = {name: os.getenv(f'{name.upper()}_EMAIL', '') for name in (*STAFF, 'Owner')}
        addresses['Shared'] = os.getenv('GOOGLE_MAIL_USER') or os.getenv('SMTP_USER', '')
        credentials = {name: (
            os.getenv(f'{name.upper()}_SMTP_USER') or os.getenv('SMTP_USER', ''),
            os.getenv(f'{name.upper()}_SMTP_PASSWORD') or os.getenv('SMTP_PASSWORD', ''),
        ) for name in STAFF}
        try:
            port = int(os.getenv('SMTP_PORT', '587'))
        except ValueError:
            raise ValueError('SMTP_PORT must be 465 or 587.') from None
        oauth = None
        method = os.getenv('GOOGLE_MAIL_AUTH', 'password').lower()
        if method not in ('password', 'oauth'):
            raise ValueError('GOOGLE_MAIL_AUTH must be oauth or password.')
        if method == 'oauth':
            from google_mail_auth import GoogleMailAuth
            oauth = GoogleMailAuth(project_dir, os.getenv('GOOGLE_MAIL_USER', ''))
        return cls(project_dir, (mode or os.getenv('STAFF_EMAIL_MODE', 'off')).lower(), addresses,
                   os.getenv('SMTP_HOST', ''), port, os.getenv('SMTP_SECURITY', 'starttls'), credentials,
                   team_updates=os.getenv('STAFF_EMAIL_TEAM_UPDATES', 'true').lower() == 'true',
                   bcc=os.getenv('OWNER_BCC_EMAIL', ''), oauth=oauth)

    def deliver(self, sender, recipients, subject, body, *, kind="message", in_reply_to=None, references=None, reply_address=None, forwarded_message=None):
        if self.mode == 'off':
            return 'Staff email is disabled.'
        if sender not in STAFF and not (sender in ('Owner', 'Shared') and kind in ('manual', 'compose')):
            raise ValueError('Unknown staff sender.')
        if reply_address is not None:
            if kind not in ('reply', 'manual', 'compose') or not valid_address(reply_address):
                raise ValueError("External addresses are supported only for inbox replies.")
            if not self.bcc:
                raise ValueError("Configure Owner BCC before replying externally.")
            recipients = ["Owner"]
        recipients = list(dict.fromkeys(recipients))
        if not recipients or any(name not in (*STAFF, "Owner") for name in recipients):
            raise ValueError('Recipients must be Owner, Morgan, Avery, Jordan, or Cameron.')
        if '\r' in subject or '\n' in subject or not subject.strip():
            raise ValueError('Email subject must be one non-empty line.')
        if self.count >= self.limit:
            return 'Run email limit reached. No message created or sent.'
        self.count += 1
        message = EmailMessage()
        if not valid_address(self.addresses.get(sender,'')):
            raise ValueError('Configure the selected sender address.')
        label = {'Owner':'CEO', 'Shared':'Shared inbox'}.get(sender,sender)
        message['From'] = formataddr((f'{label} | Onyx and Ink', self.addresses[sender]))
        message['To'] = reply_address or ', '.join(formataddr((name, self.addresses[name])) for name in recipients)
        if self.bcc:
            message['Bcc'] = self.bcc
        message['Reply-To'] = self.addresses[sender]
        message['Subject'] = subject[:200]
        message['Date'] = formatdate(localtime=True)
        message['Message-ID'] = make_msgid()
        message['Auto-Submitted'] = 'no' if kind in ('manual', 'compose') else 'auto-replied' if kind == 'reply' else 'auto-generated'
        message['X-Onyx-Ink-Kind'] = kind
        message['X-Auto-Response-Suppress'] = 'All'
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
        if references:
            message['References'] = references
        signature = ('CEO' if sender == 'Owner' else sender) + '\nOnyx and Ink'
        if kind not in ('manual', 'compose'):
            signature += f'\nAutomated staff message | Run {self.run_id}'
        message.set_content(body[:30000] + '\n\n' + signature + '\n')
        if forwarded_message is not None:
            if kind != 'forward' or recipients != ['Owner']:
                raise ValueError('Original mail can only be forwarded to Owner.')
            message.add_attachment(forwarded_message)
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f'{self.count:02d}-{sender.lower()}.eml'
        path.write_bytes(message.as_bytes())
        if self.mode == 'draft':
            return f'Email drafted locally: {path.name}. Nothing sent.'
        envelope = list(dict.fromkeys(([reply_address] if reply_address else [self.addresses[n] for n in recipients]) + ([self.bcc] if self.bcc else [])))
        return self._submit(message, path, sender, envelope)

    def send_saved(self, path):
        path = Path(path)
        # One reservation across dashboard processes or concurrent approval requests.
        with path.with_suffix('.send-lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'This draft is already being submitted. Do not resend.'
            return self._send_saved_locked(path)

    def _send_saved_locked(self, path):
        status = path.with_suffix('.status')
        current = status.read_text().strip() if status.exists() else 'draft'
        if current != 'draft':
            return 'This message is not a draft. Unconfirmed mail is never resent automatically.'
        if self.mode == 'off':
            return 'Staff email is disabled.'
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        _, from_addr = parseaddr(str(message.get('From', '')))
        sender = next((name for name, addr in self.addresses.items() if addr and addr.lower() == from_addr.lower()), None)
        if sender is None:
            raise ValueError('Unknown staff sender.')
        envelope = [addr for _, addr in getaddresses(message.get_all('To', []) + message.get_all('Cc', []) + message.get_all('Bcc', []))]
        if any(not valid_address(addr) for addr in envelope):
            raise ValueError('Message has an invalid recipient.')
        envelope = list(dict.fromkeys(envelope))
        if not envelope:
            raise ValueError('Message has no valid recipients.')
        if self.bcc and self.bcc not in envelope:
            envelope.append(self.bcc)
        try:
            fd = os.open(status, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if status.read_text().strip() != 'draft':
                return 'This message is not a draft. Unconfirmed mail is never resent automatically.'
            fd = os.open(status, os.O_WRONLY | os.O_TRUNC)
        os.write(fd, b'sending\n')
        os.close(fd)
        return self._submit(message, path, sender, envelope)

    def _submit(self, message, path, sender, envelope):
        user, password = self.credentials.get(sender, ('', ''))
        stage = "connection"
        try:
            context = ssl.create_default_context()
            if self.security == 'ssl':
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=30)
            with client:
                if self.security == 'starttls':
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                stage = "authentication"
                if self.oauth:
                    self.oauth.smtp_login(client)
                else:
                    client.login(user, password)
                stage = "message submission"
                refused = client.send_message(message, from_addr=self.addresses[sender], to_addrs=envelope)
            status = 'partially-accepted' if refused else 'accepted'
            path.with_suffix('.status').write_text(status + '\n')
            return f'Email {status} by mail server; inbox delivery is not verified.'
        except (OSError, smtplib.SMTPException, RuntimeError) as error:
            # SMTP failures may have an uncertain delivery outcome. Do not blindly retry.
            path.with_suffix('.status').write_text('delivery-unconfirmed\n')
            code = getattr(error, 'smtp_code', None)
            detail = f'{stage}: {type(error).__name__}' + (f' (SMTP {code})' if isinstance(code, int) else '')
            path.with_suffix('.diagnostic').write_text(detail + '\n')
            print('Email delivery unconfirmed during ' + detail + '. No automatic resend.')
            return 'Email delivery unconfirmed. Local message preserved; no automatic resend.'

    def report_callback(self, sender):
        def callback(output):
            if sender in self.completed:
                return
            self.completed.add(sender)
            self.reports[sender] = output.raw
            if self.team_updates and sender != 'Morgan':
                next_agent = {'Avery': 'Jordan', 'Jordan': 'Cameron', 'Cameron': 'Morgan'}[sender]
                recipients = list(dict.fromkeys([next_agent, 'Morgan']))
                try:
                    print(self.deliver(sender, recipients, f'{sender}: task handoff to {next_agent}',
                                       f'My task is complete. Use this report as relevant for your assignment.\n\n{output.raw}', kind='handoff'))
                except (ValueError, OSError):
                    print('Staff handoff could not be prepared; task execution continues.')
        return callback

    def finish(self):
        if self.mode == 'off':
            return
        for sender, report in self.reports.items():
            if sender in self.reported:
                continue
            self.reported.add(sender)
            recipients = ['Owner']
            if self.team_updates and sender == 'Morgan':
                recipients += [name for name in STAFF if name != sender]
            try:
                print(self.deliver(sender, recipients, f'{sender}: completed staff report', report, kind='report'))
            except (ValueError, OSError):
                print('Staff report email could not be prepared; local reports remain available.')

    def failure(self, reason):
        # Callers supply a fixed explanation, never raw exceptions or credentials.
        if self.mode == 'off':
            return
        self.finish()
        try:
            print(self.deliver('Morgan', ['Owner'], 'Morgan: staff run needs attention', reason, kind='failure'))
        except (ValueError, OSError):
            print('Failure notification could not be prepared.')

    def tool_for(self, sender):
        from crewai.tools import tool
        @tool(f'Email from {sender}')
        def email_staff(recipients: list[str], subject: str, body: str) -> str:
            """Email Owner or named coworkers Morgan, Avery, Jordan, Cameron.
            Use one JSON object with recipients (a list of names), subject, and body.
            Only internal configured recipients are supported. Draft mode saves without sending.
            """
            if self.tool_count >= 4:
                return "Agent email-tool limit reached; report delivery slots are reserved."
            self.tool_count += 1
            try:
                return self.deliver(sender, recipients, subject, body)
            except (ValueError, OSError):
                return 'Email not prepared. Check recipient names, subject, and local outbox access.'
        return email_staff
