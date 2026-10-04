# Onyx and Ink Staff — Groq

This project uses **Groq**, not xAI's **Grok**. Its default model is `qwen/qwen3.8-27b`, served by Groq in instruct mode for reliable visible CrewAI responses. The OpenAI-compatible Python client sends requests only to `https://api.groq.com/openai/v1`; an OpenAI API key is not required. No Gemini or LangChain integration remains in the active code.

## Get your keys

1. Sign in or create an account at https://console.groq.com.
2. Open https://console.groq.com/keys, select **Create API Key**, and name it `Onyx and Ink Staff`.
3. Copy the key into the existing `.env` file after `GROQ_API_KEY=`. Paste it without quotes or spaces. Never paste keys into chat.
4. Keep `GROQ_MODEL=qwen/qwen3.8-27b`. Check model access and actual account limits at https://console.groq.com/settings/limits. Use the Free plan if you want to avoid paid usage; don't enable paid billing for this setup.
5. Add `TAVILY_API_KEY` through dashboard Settings for current web research. Tavily’s free plan includes monthly credits and no payment card requirement. Leave it blank to disable live search; agents will continue with labeled assumptions.

Do not overwrite your existing `.env` with `.env.example`: the example contains blank keys. Old Gemini keys can remain in `.env`; the rewritten code does not use them. `.env` is excluded from Git.

## Run

From Terminal:

```bash
cd "/Users/james/Desktop/Onyx and Ink Staff"
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python main.py --check
./venv/bin/python main.py
```

`--check` builds all agents/tasks and validates configuration. It does not call external APIs or validate the key with Groq. To change the CEO request:

```bash
./venv/bin/python main.py --directive "Plan a holiday launch for personalized tumblers and shirts."
```

Add `--verbose` for detailed agent activity. Missing/invalid keys produce a configuration message; HTTP authentication, model access, quota, and connection errors produce a short explanation.

## How it works

Avery (marketing), Jordan (web development), and Cameron (legal/HR) each receive a specific task. Morgan (COO) receives all three reports and produces the final operational plan. Agents run sequentially without recursive delegation. Reports are kept under 400 words to limit context growth. CrewAI saves each completed task before starting the next:

- `reports/marketing_campaign.md`
- `reports/web_dev_specs.md`
- `reports/legal_terms.md`
- `reports/operational_plan.md`

Each new run overwrites the corresponding completed report. Existing reports from an earlier run may remain after a partial failure. The inventory tool explicitly returns SAMPLE counts, not live warehouse inventory. No storefront deployment, live inventory connection, or legal compliance verification is performed. File-writing agent tools are unnecessary because task outputs are saved by CrewAI.

## Free-tier limits

Groq currently publishes 30 requests/minute, 1,000 requests/day, 8,000 tokens/minute, and 200,000 tokens/day for this model. Your account may differ: https://console.groq.com/docs/rate-limits.

All agents and HTTP retries share a per-process request/token limiter. Default settings reserve at most 25 requests and an estimated 8,000 input/output tokens per rolling minute. Token estimates are conservative heuristics, not the exact model tokenizer; rate limits can still occur. An estimated oversized request is isolated and sent once so Groq can apply its exact tokenizer instead of being mislabeled as a connection failure. Web-search snippets and reports are bounded to keep follow-up turns inside the free-tier budget. SDK retries are bounded to three retries and honor Groq's retry delay. Agent-level task restarts are disabled.

Optional `.env` settings:

```dotenv
GROQ_RPM=25
GROQ_TPM=8000
GROQ_MAX_COMPLETION_TOKENS=1000
```

Set these based on your account's actual limits. The output cap includes reasoning tokens. Large prompts may exceed the free per-minute limit even though the model has a larger context window. Shorten the directive/report history if the program reports that a request exceeds its configured budget. Do not run multiple copies concurrently; the limiter is local to one process and doesn't track other programs or daily consumption. Free-tier pacing messages are expected.

## Tests and rollback

```bash
./venv/bin/python -m unittest discover -s tests -v
```

Tests use mocked HTTP responses, including a complete four-report crew run. They do not spend API credits. Live model behavior and credentials require your first real run.

The previous Gemini `main.py` and `requirements.txt` are backed up in `work/backups/gemini/`. Previously installed Gemini packages may remain in the virtual environment; the Groq code does not import them.

## Staff email (prepared; connection not configured)

Each agent has its own `MORGAN_EMAIL`, `AVERY_EMAIL`, `JORDAN_EMAIL`, or `CAMERON_EMAIL` identity. Set `OWNER_EMAIL` for your reports. These must be real mailboxes or sender aliases authorized by your provider; setting an address in `.env` does not create it.

Copy the staff-mail settings from `.env.example` into your existing `.env` without replacing your API keys. Keep `STAFF_EMAIL_MODE=off` until addresses are filled. `STAFF_EMAIL_MODE=draft` keeps outside/customer email in the approval queue. `STAFF_INTERNAL_EMAIL_MODE=send` sends mail addressed only to Owner or named Onyx & Ink agents automatically through the configured Google connection. `--check` validates configuration and never sends mail.

With email enabled, Avery sends a completion handoff to Jordan and Morgan, Jordan to Cameron and Morgan, and Cameron to Morgan. Routine completion reports stay in the dashboard instead of filling the CEO inbox. Agents email Owner only for an urgent risk, a blocking decision, a failure, or one specific answer needed to finish an assignment. Set `IMPORTANT_CC_EMAIL` to copy a second human on those important Owner messages; normal handoffs and reports do not copy that address. The existing Owner BCC is limited to important Owner mail and outside communication. Set `STAFF_EMAIL_TEAM_UPDATES=false` to disable automatic handoffs. Internal messages send automatically when `STAFF_INTERNAL_EMAIL_MODE=send`; arbitrary outside addresses remain approval drafts.

The normal crew run sends outgoing reports. The separate `inbox_monitor.py` program monitors the shared inbox and handles replies while running. Neither program installs a background service. CrewAI task context still supplies reports to Morgan directly.

The SMTP transport supports an authenticated mail service using either STARTTLS on port 587 or implicit TLS on port 465. In `send` mode set `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, and a shared `SMTP_USER`/`SMTP_PASSWORD` for an account authorized to send from all four aliases. Alternatively set each agent's `<NAME>_SMTP_USER` and `<NAME>_SMTP_PASSWORD`. Never paste credentials into chat. Gmail/Workspace may support app passwords depending on account policy. Microsoft 365 setups generally need OAuth or another approved transport; this code does not implement OAuth yet. Choose your provider before configuring sending.

Each message is saved locally before transmission. A `.status` file records server acceptance or an unconfirmed outcome. SMTP server acceptance does not verify inbox delivery. Unconfirmed or partial sends are **not** automatically resent, because some recipients may already have received the message. Delivery problems do not discard task reports. Agent Markdown is converted into a clean plain-text part and a styled HTML alternative before delivery.

Provider setup references:
- Gmail authorized sender aliases: https://support.google.com/mail/answer/22370
- Microsoft SMTP OAuth: https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth

### Your Google Workspace setup

Configured sender identities:

| Agent | Address |
|---|---|
| Morgan | coo@onyxandink.org |
| Avery | marketing@onyxandink.org |
| Jordan | it@onyxandink.org |
| Cameron | hr@onyxandink.org |

The four staff addresses and `ceo@onyxandink.org` are aliases on the authorized Google Workspace account. Mail authentication now uses Google OAuth. `OWNER_BCC_EMAIL` is used for outside communication and important Owner notifications; `IMPORTANT_CC_EMAIL` receives only important Owner notifications.

For four separate Workspace accounts, each account needs its own allowed SMTP authentication credentials. For aliases, first create the four aliases on the real Workspace account and authorize them in Gmail’s **Send mail as** settings; configure the actual account as `SMTP_USER`. Aliases do not have separate logins or inboxes. Merely editing `.env` does not create mailboxes or sender permissions.

Where your Google account policy permits app passwords:
1. Enable 2-Step Verification on the sending account.
2. Open https://myaccount.google.com/apppasswords and create an app password for Onyx and Ink Staff.
3. Paste it **locally** into `.env`, without spaces. Use `SMTP_PASSWORD` for the shared alias account, or the relevant `<NAME>_SMTP_PASSWORD` for separate accounts. Do not use the account’s regular password.
4. Set the corresponding SMTP username locally, and the Owner recipient once confirmed.
5. Use `STAFF_EMAIL_MODE=draft`, then run `./venv/bin/python main.py --check`. A normal crew run will produce local email drafts alongside the reports.
6. When account permissions and credentials are ready, set `STAFF_EMAIL_MODE=send`. Subsequent normal runs autonomously send the authorized reports and internal messages. `--check` still sends nothing and does not verify authentication.

If app passwords are unavailable under your Workspace policy, leave sending off and use an OAuth integration or admin-approved relay instead. This version does not implement Google OAuth. Google’s app-password instructions: https://support.google.com/accounts/answer/185833.

Owner-alias setup: sign in to https://admin.google.com with your Workspace administrator account, open Directory → Users → the account that owns the aliases → Alternate email addresses, and add `owner` for `onyxandink.org`. This is a proposed alias; it must be created in Google Workspace and authorized for sending before use.

## Inbox monitoring and replies

An authenticated reply from Owner to a staff alias is added to that agent’s shared conversation history. The agent incorporates the answer, can execute the same bounded Workspace/research/design actions available in dashboard chat, and emails the result back automatically. If another decision still blocks the assignment, the agent may send one focused follow-up question. Outside-customer replies and new outside messages continue to require approval when `STAFF_EMAIL_MODE=draft`.

The monitor uses the one real Workspace account owning all aliases:

```dotenv
IMAP_HOST=imap.gmail.com
IMAP_USER=
IMAP_PASSWORD=
INBOX_POLL_SECONDS=60
INBOX_BATCH_LIMIT=5
```

Blank IMAP credentials fall back to shared `SMTP_USER` and `SMTP_PASSWORD`. Enter the full real account email address and app password locally. Google Workspace must permit this authentication and IMAP access. If app passwords are unavailable, this implementation needs an OAuth extension; do not substitute the normal account password.

After credentials and aliases are configured:

```bash
./venv/bin/python inbox_monitor.py --check
./venv/bin/python inbox_monitor.py --once
./venv/bin/python inbox_monitor.py
```

`--check` validates settings without accessing Gmail or Groq. The **first connected check saves a baseline and processes no old mail**. Send a fresh message to a staff alias after that check. The normal monitor checks every 60 seconds until Control-C; it requires the Mac and process to remain running. Start this before sending assignments. Do not run a second monitor or a separate crew simultaneously: Groq quotas are account-wide, while request pacing is process-local.

Incoming messages addressed to a staff alias are routed to that agent. Mail to the proposed Owner alias is handled by Morgan. Multi-alias messages receive one reply from the first addressed staff alias. Internal and outside/customer senders are eligible. External replies go only to the original From address and always BCC Owner’s personal address; Reply-To cannot redirect them. Outside mail receives a fixed acknowledgment escalating it to the CEO, plus a separate forward of the original email (including attachments) to ceo@onyxandink.org. No model-generated answer is sent externally. The acknowledgment preserves the original thread. The first Gmail Authentication-Results header must show aligned DMARC pass before an eligible message can produce a reply.

Incoming content is treated as untrusted data. The reply agent has no tools: it can answer questions, propose designs/plans, or ask for missing information, but cannot execute code or act on a store. Internal reply generation does not read attachments or HTML-only messages. External forwarding preserves these in the attached original email. Email input is capped to limit Groq context; full historical threads are not fetched separately.

Automated reports, failure notices, mailing lists, bounces, and auto-replies are ignored. Recognized internal handoffs can receive one response; the response is marked `Auto-Submitted: auto-replied` and will not produce another reply. Thread headers (`In-Reply-To` and `References`) are preserved for ordinary mail. Local UID/Message-ID tracking prevents repeat replies across polls and restarts, and an exclusive lock prevents two monitors for the same project.

Each eligible message is reserved locally **before** generation or delivery. Failed or interrupted processing is marked reserved/needs-review, rather than silently retried and potentially sent twice. Drafted replies are considered processed too: switching to send mode does not automatically send old drafts. Review drafts and unconfirmed deliveries locally; a new human message can request another answer. State is in `work/inbox-monitor.sqlite3`; deleting it resets the baseline on the next start, not a replay of old mail.

Gmail's IMAP connection is encrypted on port 993: https://developers.google.com/workspace/gmail/imap/imap-smtp.

## Google sign-in setup (current configuration)

The project now uses `GOOGLE_MAIL_AUTH=oauth` and `GOOGLE_MAIL_USER=inbox@onyxandink.org`. SMTP/IMAP password fields are unused in this mode. The older app-password instructions above apply only to password mode.

1. Sign in to https://console.cloud.google.com/ with your Workspace account and create/select a project named Onyx and Ink Staff.
2. Enable the Gmail API in APIs & Services → Library. Setup checks the signed-in mailbox through the Gmail profile endpoint.
3. In Google Auth Platform, configure Branding and Audience. Choose Internal if available for your Workspace organization. If External is required, add `inbox@onyxandink.org` as a test user. External Testing authorizations with mail scopes normally expire after seven days, so this is unsuitable for unattended long-term operation.
4. In Data Access, add `https://mail.google.com/`. IMAP/SMTP require this broad mail scope. The code does not delete mail. Workspace administrators may need to permit this client and IMAP access; OAuth does not override organization policy.
5. In Clients, create an OAuth client with application type **Desktop app**. Download its JSON and save it in this project as `google-oauth-client.json`. Do not paste its contents into chat.
6. Run the following from the project folder:

```bash
./venv/bin/python google_mail_auth.py
```

Sign in as `inbox@onyxandink.org` and authorize access. Setup confirms the mailbox matches that account before saving `work/google-mail-token.json` with owner-only file permissions. The client file and token directory are ignored by Git. The monitor refreshes expiring access tokens automatically; revoked/expired refresh authorization requires repeating setup.

Then keep `STAFF_EMAIL_MODE=draft` and run:

```bash
./venv/bin/python main.py --check
./venv/bin/python inbox_monitor.py --check
./venv/bin/python inbox_monitor.py --once
./venv/bin/python inbox_monitor.py
```

The first connected poll establishes a baseline. Send fresh test mail to a staff alias and review the local drafts. After confirming aliases (including `ceo@onyxandink.org`) work, set `STAFF_EMAIL_MODE=send` and restart the monitor. Authorization itself sends no email.

Google references: https://developers.google.com/identity/protocols/oauth2/native-app and https://developers.google.com/workspace/gmail/imap/xoauth2-protocol.

The inbox monitor skips messages larger than 25 MB; internal model input remains capped at 12,000 characters.

## Local staff dashboard

Run `./venv/bin/python dashboard.py` in another Terminal while keeping the inbox monitor running. Open http://127.0.0.1:8765. The dashboard shows configured email mode, monitor lock status, saved authorization, named staff, the latest 60 saved outgoing messages, delivery diagnostics, and saved reports. Click a message to read its local draft/sent copy. Mailbox arrival is not verified by an SMTP accepted status. It does not fetch extra mailbox contents or call models to populate the dashboard.

A monitor already running in Terminal must be stopped there with Control-C. After stopping it, you can manage a new monitor from the dashboard. Mode changes require stopping the monitor first because settings are loaded at startup. Live sending and start controls describe their effects before you confirm. Old local drafts are never automatically sent. Stop a dashboard-managed monitor before closing the dashboard server; the monitor is a separate process. The dashboard is local to this Mac, is not a background service, and does not provide access from other devices. Keep the server running while using it.

## Persistent dashboard and simple inbox

The macOS per-user service starts the dashboard at login and restarts it after a crash. Install with `./venv/bin/python install_dashboard_service.py`. It writes `~/Library/LaunchAgents/org.onyxandink.staff-dashboard.plist`. Dashboard and monitor logs are under `work/`. The monitor’s enabled/disabled choice is stored in `work/monitor-enabled.json`; the dashboard supervises it while enabled and does not start duplicates. Start/Stop can also control an existing monitor holding this project's lock. Stopping waits for it to exit; changing email mode requires it to be stopped first.

The service survives closing Terminal and the browser. It runs while your user is logged in and the Mac is awake. It cannot work while the Mac is off, asleep, offline, or logged out. It resumes at login and after waking; strict uptime needs an always-on host. No sleep or security settings are changed by installation.

Use Refresh inbox to load the latest 25 emails from Gmail. Open a message to read its text and attachment names. HTML is converted to text; remote images and scripts are never rendered. Opening here does not mark Gmail messages read. Open attachments in Gmail. Reply as CEO or a named agent; recipients come from the original sender, and personal Gmail is BCC'd. Draft mode saves locally; Live sends after your click and confirmation. Manual replies do not use Groq. Manual send reservations prevent duplicate delivery on retries; unconfirmed outcomes require review, not automatic resend. Existing autonomous monitoring can still reply separately; Stop it before manually handling mail if you want to avoid both responses.

The inbox uses the existing Gmail authorization. If Google revokes or expires it, rerun `google_mail_auth.py`. The dashboard is bound to localhost and is only reachable on this Mac. To uninstall the background dashboard, run `launchctl bootout gui/$(id -u)/org.onyxandink.staff-dashboard` and remove its LaunchAgents plist. Stop the monitor in the dashboard first.

## Website pages, inbox switching, and delivery tracking

The staff site now has separate URLs: `/overview`, `/inbox`, `/activity`, `/reports`, and `/settings`. Navigation shows only the selected page. Inbox has a switcher for All, Shared, CEO, Morgan, Avery, Jordan, and Cameron. These are filtered views of the one authorized Gmail account, not separate Google accounts. Alias views search To, Cc, and Delivered-To; the All view includes mail sent directly to the real account. Selecting an alias also chooses that sender for the reply form.

On Delivery & activity, click Check delivery to inspect the latest 10 saved send attempts. The site matches exact Message-ID values to Gmail Sent copies and looks for human replies in the matching Gmail thread. It also checks the latest 20 mailer-daemon/postmaster notices from the last 30 days for structured delivery failures referencing the original message. Evidence is saved locally beside the outgoing email as `.receipt.json`. A failure notice may concern one recipient in a multi-recipient email; a missing failure notice is not proof of delivery. Gmail Sent confirms that a sent copy exists, not that the destination inbox received or read it. Recipient reply received indicates a matching response from an original To recipient; it is not a read receipt. Unconfirmed attempts are never resent by checking delivery.

### Wi-Fi website and agent chat

The Mac continues serving the local dashboard at http://127.0.0.1:8765. Wi-Fi access uses HTTPS on port 8766 with a shared password. On the Mac, open Settings to see the Wi-Fi address and reveal the password. Download the local trust certificate there, transfer it to each authorized device, and install/trust it before opening the HTTPS address. On iPhone/iPad, install the downloaded profile under Settings, then enable its root certificate under General → About → Certificate Trust Settings. Share the password only with people authorized to read and send business email. The Mac must remain powered on, awake, and connected; the login service starts after signing in. Reserve its IP address in your router if the address changes.

Inbox → New email lets you choose CEO, shared inbox, or a staff sender. Staff reports have an agent dropdown. Agent chat keeps a separate saved conversation for Morgan, Avery, Jordan, and Cameron, shared between the dashboard and paired Telegram accounts. Chat drafts and discusses work; it does not execute email or storefront actions. Messages are sent to Groq for responses.

For Telegram, privately message the official @BotFather, use /newbot, and copy the resulting bot token into dashboard Settings (do not paste it into an AI conversation). Enable Telegram and save, then create a pairing code. Open your new bot’s private chat and send the displayed /pair command within ten minutes. Use /morgan, /avery, /jordan, or /cameron to select an agent. Only paired private accounts can use it. Disable Telegram in Settings to stop polling. Pairing grants access to the shared conversations; chat history stays on this Mac. Telegram must be reachable and the Mac awake.

### GitHub code backup

GitHub receives application code, dashboard assets, tests, dependency requirements, and this guide. Local `.env` credentials, Google authorization, Wi-Fi passwords/private keys, emails, chat history, generated reports, temporary files, and the Python environment are excluded. This is a code backup; those private runtime files require a separate secure backup.

`github_sync.py` checks exportable files for configured secrets and common key formats before committing and pushing to `main`. It refuses unexpected tracked files, symlinks, existing staged work, credentials embedded in a remote URL, or remote changes requiring a merge. It never force pushes. Automated checks run through the Codex recurring task once GitHub setup is finished. The Mac and Codex must be available for scheduled runs; offline changes sync when a later run succeeds.


## Actions from staff chat

Open **Agent chat** in the toolbar or select a staff member. The drawer stays closed on other pages until opened. All four staff agents can create local reports, prepare email, generate Cloudflare designs, research public websites through Tavily, create Google Docs/Sheets/Slides, upload generated files to Drive, and create events on the connected account's primary calendar. Chat requests run a maximum of six reasoning steps; completed actions appear under Created files. Mail to named staff or the CEO sends automatically; outside mail waits in Outbox for **Approve & send**. Files created through Telegram chat are also sent back to the paired private conversation.

Configure Cloudflare on the host Mac in **Settings → Cloudflare designs**. Use a Workers AI token, account ID, and the Workers Free plan. Generation has a conservative local cap of 50 attempts/day (UTC), counts uncertain requests, and never switches to a paid provider. Both models share Cloudflare's account-wide allowance; other account usage can exhaust it sooner. Schnell is the default. Klein supports editing a previously generated design using its file ID; live rendering requires provider validation after credentials are entered. Generated designs require checking dimensions, spelling, and resolution against the physical product before printing.

Use **Settings → Connect Google Workspace** on the host Mac. For automatic Workspace creation, enable Google Drive, Docs, Sheets, Slides, and Calendar APIs in the existing OAuth client project, then run:

```bash
./venv/bin/python workspace_tools.py --authorize
```

Sign in as the shared Workspace account. Mail authorization is separate. Staff access is limited to files the application creates or is explicitly granted through drive.file, and owned calendar events. No calendar invitations are sent. Existing documents are not overwritten by chat; it currently creates new files.

**Settings → Check connection** verifies SMTP authentication without sending email. A saved draft must be approved to test actual delivery. SMTP acceptance and Gmail Sent records do not prove delivery to the recipient. Failed or uncertain old attempts are preserved and never automatically resent.


## Inbox cleanup, attachments, and deletion

Inbox messages load in pages of 40 with search across loaded messages. Replies and new emails support up to five uploaded files (10 MB each, 15 MB combined); incoming attachments can be downloaded directly. The chat drawer includes an agent selector.

Dashboard cleanup hides messages older than 30 days by default. Change this to Never, 7, 30, or 90 days in Settings. Hidden messages remain in Gmail; use the Hidden or All inbox view to find and restore them. Hide/restore/automatic cleanup never archive, mark, or delete Gmail messages. **Delete from Gmail** requires confirmation and moves the selected message to Gmail Trash. Agents can only delete mail in their own inbox after the exact direct command `delete email MESSAGE_ID`; quoted email content and inferred instructions cannot authorize deletion. Permanent Gmail deletion is not implemented.

Nightly backups run at 3 a.m. Eastern using the existing desktop automation. Each source update includes a dated CHANGELOG.md entry; credentials, email, chats, reports, uploads, and certificates remain excluded. Existing human staging and remote conflicts stop the backup for review.
