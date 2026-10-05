"""Named staff mailboxes, internal-only mail tools, and autonomous report delivery."""
import os
import html
import json
import re
import smtplib
import ssl
import threading
import uuid
import fcntl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path

STAFF = ('Morgan', 'Avery', 'Jordan', 'Cameron')
INTERNAL_RECIPIENTS = (*STAFF, 'Owner', 'Jaunee')
RECIPIENT_LABELS = {'Owner':'James | CEO', 'Jaunee':'Jaunee | Vice President'}
SIGNATURE_TITLES = {
    'Morgan':'Chief Operating Officer',
    'Avery':'Marketing Lead',
    'Jordan':'IT & Storefront Development Lead',
    'Cameron':'Legal & HR Lead',
}


def valid_address(value):
    return bool(re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value)) and value.isascii()


def default_signatures(addresses,website='https://onyxandink.org'):
    website=(website or 'https://onyxandink.org').strip().rstrip('/')
    return {name:f'{name}\n{SIGNATURE_TITLES[name]}\nOnyx & Ink\n{addresses.get(name,"")} | {website}' for name in STAFF}


def validate_signatures(values):
    if not isinstance(values,dict) or set(values)!=set(STAFF):raise ValueError('Provide one signature for each agent.')
    cleaned={}
    for name in STAFF:
        value=values[name]
        if not isinstance(value,str) or not value.strip() or len(value)>1200 or '\x00' in value:raise ValueError('Each signature must contain 1–1,200 characters.')
        cleaned[name]=value.replace('\r\n','\n').replace('\r','\n').strip()
    return cleaned


def signature_settings(project_dir,addresses,website='https://onyxandink.org'):
    defaults=default_signatures(addresses,website);path=Path(project_dir)/'work'/'email-signatures.json'
    try:return validate_signatures(json.loads(path.read_text()))
    except (OSError,ValueError,TypeError):return defaults


def save_signature_settings(project_dir,values):
    values=validate_signatures(values);path=Path(project_dir)/'work'/'email-signatures.json';path.parent.mkdir(exist_ok=True)
    temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');temp.write_text(json.dumps(values,indent=2));temp.chmod(0o600);temp.replace(path);return values


def reset_signature_settings(project_dir,addresses,website='https://onyxandink.org'):
    path=Path(project_dir)/'work'/'email-signatures.json'
    try:path.unlink()
    except FileNotFoundError:pass
    return default_signatures(addresses,website)


def markdown_to_plain(value):
    """Turn common model Markdown into a readable plain-text email body."""
    text=str(value).replace('\r\n','\n').replace('\r','\n')
    text=re.sub(r'```(?:[\w+-]+)?\n?', '', text)
    text=re.sub(r'!\[([^]]*)\]\([^)]+\)', r'\1', text)
    text=re.sub(r'\[([^]]+)\]\((https?://[^)]+)\)', r'\1 (\2)', text)
    text=re.sub(r'^\s{0,3}#{1,6}\s+', '', text, flags=re.M)
    text=re.sub(r'^\s*[-*+]\s+', '• ', text, flags=re.M)
    text=re.sub(r'^\s*>\s?', '', text, flags=re.M)
    text=re.sub(r'(\*\*|__)(.+?)\1', r'\2', text)
    text=re.sub(r'(?<!\*)\*([^*\n]+)\*', r'\1', text)
    text=re.sub(r'(?<!_)_([^_\n]+)_', r'\1', text)
    text=re.sub(r'`([^`]+)`', r'\1', text)
    text=re.sub(r'^\s*(?:---+|___+|\*\*\*+)\s*$', '', text, flags=re.M)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def markdown_to_html(value):
    """Render a safe, small Markdown subset for Gmail and other mail clients."""
    def inline(raw):
        escaped=html.escape(raw,quote=True)
        escaped=re.sub(r'\[([^]]+)\]\((https?://[^)]+)\)',r'<a href="\2" style="color:#0071e3">\1</a>',escaped)
        escaped=re.sub(r'(\*\*|__)(.+?)\1',r'<strong>\2</strong>',escaped)
        escaped=re.sub(r'`([^`]+)`',r'<code style="background:#f1f1f3;padding:2px 5px;border-radius:5px">\1</code>',escaped)
        escaped=re.sub(r'(?<!\*)\*([^*]+)\*',r'<em>\1</em>',escaped)
        return escaped
    lines=str(value).replace('\r\n','\n').replace('\r','\n').split('\n')
    output=[];list_type=None;in_code=False;code=[]
    def close_list():
        nonlocal list_type
        if list_type:output.append(f'</{list_type}>');list_type=None
    for raw in lines:
        if raw.strip().startswith('```'):
            if in_code:
                output.append('<pre style="white-space:pre-wrap;background:#f5f5f7;padding:12px;border-radius:10px">'+html.escape('\n'.join(code))+'</pre>');code=[]
            in_code=not in_code;continue
        if in_code:code.append(raw);continue
        heading=re.match(r'^\s{0,3}(#{1,3})\s+(.+)$',raw)
        bullet=re.match(r'^\s*[-*+]\s+(.+)$',raw)
        numbered=re.match(r'^\s*\d+[.)]\s+(.+)$',raw)
        if heading:
            close_list();level=len(heading.group(1))+1;output.append(f'<h{level} style="margin:22px 0 8px">{inline(heading.group(2))}</h{level}>')
        elif bullet or numbered:
            wanted='ul' if bullet else 'ol'
            if list_type!=wanted:close_list();list_type=wanted;output.append(f'<{wanted} style="padding-left:24px">')
            output.append('<li style="margin:6px 0">'+inline((bullet or numbered).group(1))+'</li>')
        elif not raw.strip():
            close_list()
        else:
            close_list();output.append('<p style="margin:0 0 12px">'+inline(raw.strip())+'</p>')
    close_list()
    if code:output.append('<pre style="white-space:pre-wrap;background:#f5f5f7;padding:12px;border-radius:10px">'+html.escape('\n'.join(code))+'</pre>')
    return ''.join(output)


class StaffMail:
    def __init__(self, project_dir, mode='off', addresses=None, host='', port=587,
                 security='starttls', credentials=None, limit=12, team_updates=True, bcc='', oauth=None,
                 signatures=None, internal_mode=None, important_cc=''):
        if mode not in ('off', 'draft', 'send'):
            raise ValueError('STAFF_EMAIL_MODE must be off, draft, or send.')
        internal_mode = ('draft' if mode == 'off' else mode) if internal_mode is None else internal_mode
        if internal_mode not in ('draft','send'):
            raise ValueError('STAFF_INTERNAL_EMAIL_MODE must be draft or send.')
        self.oauth = oauth
        self.mode = mode
        self.internal_mode = internal_mode
        self.addresses = addresses or {}
        self.host, self.port, self.security = host, port, security
        self.credentials = credentials or {}
        self.limit, self.count = limit, 0
        self.team_updates = team_updates
        self.bcc = bcc
        self.important_cc = important_cc
        self.signatures = signatures or {}
        self.run_id = uuid.uuid4().hex
        self.outbox = Path(project_dir) / 'work' / 'email-outbox' / self.run_id
        self.completed = set()
        self.reports = {}
        self.reported = set()
        self.tool_count = 0
        self.lock = threading.RLock()
        if mode == 'off':
            return
        if bcc and not valid_address(bcc):
            raise ValueError('Set a valid OWNER_BCC_EMAIL address.')
        if important_cc and not valid_address(important_cc):
            raise ValueError('Set a valid IMPORTANT_CC_EMAIL address.')
        for name in (*STAFF, 'Owner'):
            if not valid_address(self.addresses.get(name, '')):
                raise ValueError(f'Set a valid {name.upper()}_EMAIL address before enabling staff email.')
        if self.addresses.get('Jaunee') and not valid_address(self.addresses['Jaunee']):
            raise ValueError('Set a valid IMPORTANT_CC_EMAIL address for Jaunee.')
        if len({self.addresses[n].lower() for n in STAFF}) != len(STAFF):
            raise ValueError('Each agent needs a distinct mailbox or authorized alias.')
        if mode == 'send' or internal_mode == 'send':
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
    def from_env(cls, project_dir, mode=None, config=None):
        get = os.getenv if config is None else config.get
        addresses = {name: get(f'{name.upper()}_EMAIL', '') for name in (*STAFF, 'Owner')}
        addresses['Jaunee'] = get('IMPORTANT_CC_EMAIL', '')
        addresses['Shared'] = get('GOOGLE_MAIL_USER') or get('SMTP_USER', '')
        credentials = {name: (
            get(f'{name.upper()}_SMTP_USER') or get('SMTP_USER', ''),
            get(f'{name.upper()}_SMTP_PASSWORD') or get('SMTP_PASSWORD', ''),
        ) for name in STAFF}
        try:
            port = int(get('SMTP_PORT', '587'))
        except ValueError:
            raise ValueError('SMTP_PORT must be 465 or 587.') from None
        oauth = None
        method = get('GOOGLE_MAIL_AUTH', 'password').lower()
        if method not in ('password', 'oauth'):
            raise ValueError('GOOGLE_MAIL_AUTH must be oauth or password.')
        if method == 'oauth':
            from google_mail_auth import GoogleMailAuth
            oauth = GoogleMailAuth(project_dir, get('GOOGLE_MAIL_USER', ''))
        website=(get('BUSINESS_WEBSITE') or 'https://onyxandink.org').strip().rstrip('/')
        signatures=signature_settings(project_dir,addresses,website)
        return cls(project_dir, (mode or get('STAFF_EMAIL_MODE', 'off')).lower(), addresses,
                   get('SMTP_HOST', ''), port, get('SMTP_SECURITY', 'starttls'), credentials,
                   team_updates=get('STAFF_EMAIL_TEAM_UPDATES', 'true').lower() == 'true',
                   bcc=get('OWNER_BCC_EMAIL', ''), oauth=oauth, signatures=signatures,
                   internal_mode=get('STAFF_INTERNAL_EMAIL_MODE','draft').lower(),
                   important_cc=get('IMPORTANT_CC_EMAIL',''))

    def deliver(self, sender, recipients, subject, body, *, kind="message", in_reply_to=None, references=None, reply_address=None, forwarded_message=None, attachments=None, important=False):
        with self.lock:
            return self._deliver(sender,recipients,subject,body,kind=kind,in_reply_to=in_reply_to,references=references,reply_address=reply_address,forwarded_message=forwarded_message,attachments=attachments,important=important)

    def _deliver(self, sender, recipients, subject, body, *, kind="message", in_reply_to=None, references=None, reply_address=None, forwarded_message=None, attachments=None, important=False):
        if self.mode == 'off':
            return 'Staff email is disabled.'
        if sender not in STAFF and not (sender in ('Owner', 'Shared') and kind in ('manual', 'compose')):
            raise ValueError('Unknown staff sender.')
        if reply_address is not None:
            if kind not in ('reply', 'manual', 'compose') or not valid_address(reply_address):
                raise ValueError("External addresses are supported only for inbox replies.")
            if not self.bcc:
                raise ValueError("Configure the CEO BCC before replying externally.")
            recipients = ["Owner"]
        recipients = ['Owner' if name in ('James','CEO') else name for name in recipients]
        recipients = list(dict.fromkeys(recipients))
        if not recipients or any(name not in INTERNAL_RECIPIENTS or not valid_address(self.addresses.get(name,'')) for name in recipients):
            raise ValueError('Recipients must be James, Jaunee, Morgan, Avery, Jordan, or Cameron.')
        if '\r' in subject or '\n' in subject or not subject.strip():
            raise ValueError('Email subject must be one non-empty line.')
        if self.count >= self.limit:
            return 'Run email limit reached. No message created or sent.'
        self.count += 1
        message = EmailMessage()
        if not valid_address(self.addresses.get(sender,'')):
            raise ValueError('Configure the selected sender address.')
        label = {'Owner':'James | CEO', 'Shared':'Shared inbox'}.get(sender,sender)
        message['From'] = formataddr((f'{label} | Onyx and Ink', self.addresses[sender]))
        message['To'] = reply_address or ', '.join(formataddr((RECIPIENT_LABELS.get(name,name), self.addresses[name])) for name in recipients)
        copy_owner = important and 'Owner' in recipients and reply_address is None
        copy_bcc = self.bcc if reply_address is not None or copy_owner else ''
        copy_cc = self.important_cc if copy_owner and 'Jaunee' not in recipients else ''
        if copy_cc:message['Cc'] = formataddr((RECIPIENT_LABELS['Jaunee'],copy_cc))
        if copy_bcc:message['Bcc'] = copy_bcc
        message['Reply-To'] = self.addresses[sender]
        message['Subject'] = subject[:200]
        message['Date'] = formatdate(localtime=True)
        message['Message-ID'] = make_msgid()
        message['Auto-Submitted'] = 'no' if kind in ('manual', 'compose') else 'auto-replied' if kind == 'reply' else 'auto-generated'
        message['X-Onyx-Ink-Kind'] = kind
        message['X-Onyx-Ink-Run-ID'] = self.run_id
        message['X-Auto-Response-Suppress'] = 'All'
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
        if references:
            message['References'] = references
        signature = self.signatures.get(sender,('James\nChief Executive Officer' if sender == 'Owner' else 'Onyx & Ink Team') + '\nOnyx & Ink')
        clean_body=markdown_to_plain(body[:30000])
        message.set_content(clean_body + '\n\n' + signature + '\n')
        signature_html='<br>'.join(html.escape(line) for line in signature.splitlines())
        message.add_alternative(
            '<div style="font:15px/1.55 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#1d1d1f;max-width:680px">'
            +markdown_to_html(body[:30000])
            +'<div style="margin-top:28px;padding-top:16px;border-top:1px solid #dedee3;color:#6e6e73">'+signature_html+'</div></div>',
            subtype='html',
        )
        if forwarded_message is not None:
            if kind != 'forward' or recipients != ['Owner']:
                raise ValueError('Original mail can only be forwarded to James, the CEO.')
            message.add_attachment(forwarded_message)
        attachment_total = 0
        for attachment in attachments or []:
            attachment = Path(attachment).resolve()
            artifact_root = (Path(self.outbox).parents[1] / 'artifacts').resolve()
            upload_root = (Path(self.outbox).parents[1] / 'uploads').resolve()
            if not (attachment.is_relative_to(artifact_root) or attachment.is_relative_to(upload_root)) or not attachment.is_file() or attachment.stat().st_size > 15_000_000:
                raise ValueError('Only generated staff files can be attached (15 MB maximum).')
            attachment_total += attachment.stat().st_size
            if attachment_total > 15_000_000:raise ValueError('Combined attachments exceed 15 MB.')
            import mimetypes
            mime = mimetypes.guess_type(attachment.name)[0] or 'application/octet-stream'
            major, minor = mime.split('/', 1)
            message.add_attachment(attachment.read_bytes(), maintype=major, subtype=minor, filename=attachment.name.split('--',1)[-1])
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f'{self.count:02d}-{sender.lower()}.eml'
        path.write_bytes(message.as_bytes())
        delivery_mode = self.internal_mode if reply_address is None else self.mode
        if delivery_mode == 'draft':
            return f'Email drafted locally: {path.name}. Nothing sent.'
        envelope = list(dict.fromkeys(([reply_address] if reply_address else [self.addresses[n] for n in recipients]) + ([copy_cc] if copy_cc else []) + ([copy_bcc] if copy_bcc else [])))
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
                subjects = {'Avery':'Marketing update and next steps','Jordan':'Storefront update and next steps','Cameron':'Policy update and next steps'}
                introductions = {
                    'Avery':'Hi Jordan and Morgan,\n\nI finished this round of marketing work. The updated Marketing report is ready in Dashboard → Staff Reports. Jordan, please review the product, audience, and offer details there as you continue the storefront work.',
                    'Jordan':'Hi Cameron and Morgan,\n\nI finished this round of storefront work. The updated Storefront report is ready in Dashboard → Staff Reports. Cameron, please review any customer-facing policy points there that affect the experience.',
                    'Cameron':'Hi Morgan,\n\nI finished this round of policy work. The updated Policy report is ready in Dashboard → Staff Reports, including any decisions that still need attention.',
                }
                try:
                    print(self.deliver(sender, recipients, subjects[sender],
                                       introductions[sender], kind='message'))
                except (ValueError, OSError):
                    print('Staff update could not be prepared; work continues.')
        return callback

    def finish(self):
        # Routine reports stay in the dashboard. Email is reserved for blockers,
        # failures, and questions that require human input.
        self.reported.update(self.reports)

    def failure(self, reason):
        # Callers supply a fixed explanation, never raw exceptions or credentials.
        if self.mode == 'off':
            return
        self.finish()
        try:
            print(self.deliver('Morgan', ['Owner'], 'Morgan: staff run needs attention', reason, kind='failure', important=True))
        except (ValueError, OSError):
            print('Failure notification could not be prepared.')

    def tool_for(self, sender, allow_owner=True):
        from crewai.tools import tool
        @tool(f'Email from {sender}')
        def email_staff(recipients: list[str], subject: str, body: str) -> str:
            """Email James (CEO), Jaunee (Vice President), or named coworkers Morgan, Avery, Jordan, Cameron.
            Use one JSON object with recipients (a list of names), subject, and body.
            Only internal configured recipients are supported. Draft mode saves without sending.
            Address James as CEO and Jaunee as Vice President in executive correspondence.
            Write as a friendly coworker with a natural subject and greeting. Do not use workflow jargon
            such as task handoff or artifact. Do not add a sign-off; one is appended.
            """
            if self.tool_count >= 4:
                return "Agent email-tool limit reached; report delivery slots are reserved."
            recipients=['Owner' if name in ('James','CEO') else name for name in recipients]
            if 'Owner' in recipients and not allow_owner:
                return 'Coordinate this question with Morgan. Morgan will decide whether CEO input is required.'
            self.tool_count += 1
            try:
                return self.deliver(sender, recipients, subject, body, important='Owner' in recipients)
            except (ValueError, OSError):
                return 'Email not prepared. Check recipient names, subject, and local outbox access.'
        return email_staff
