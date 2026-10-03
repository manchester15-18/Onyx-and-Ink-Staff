(()=>{
const $=id=>document.getElementById(id);
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const on=(id,ev,fn)=>$(id).addEventListener(ev,fn);

if($('loginForm')){
  on('loginForm','submit',async e=>{e.preventDefault();
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('loginPassword').value})});
    const d=await r.json();if(r.ok)location.reload();else $('loginNotice').textContent=d.error});
  return;
}

const PAGES=['overview','inbox','activity','reports','settings'];
let S=null,page='',agent='Morgan',inboxLoaded=false,inboxBusy=false,opened=null,replyId=null,composeId=null,filt='all',repFilter='all',repIdx=null,chatBusy=false;
const LABEL={accepted:'Google accepted','delivery-unconfirmed':'Unconfirmed','partially-accepted':'Some refused',draft:'Draft'};

async function api(path,data={}){
  const r=await fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':S.token},body:JSON.stringify(data)});
  const j=await r.json();if(!r.ok)throw Error(j.error||'Request failed.');return j}
function say(msg){$('notice').textContent=msg}
function detail(t,m,b){$('detailTitle').textContent=t;$('detailMeta').textContent=m;$('detailBody').textContent=b;$('detail').showModal()}

/* Agent status is derived from real data only: saved drafts, unconfirmed sends, monitor state. */
function agentState(name){
  const mine=S.activity.filter(a=>a.sender.startsWith(name));
  const bad=mine.filter(a=>a.status==='delivery-unconfirmed'||a.status==='partially-accepted').length;
  const drafts=mine.filter(a=>a.status==='draft').length;
  if(bad)return['need',bad+' unconfirmed send'+(bad>1?'s':'')];
  if(drafts)return['busy',drafts+' draft'+(drafts>1?'s':'')+' saved'];
  return S.monitor?['live','Monitor running']:['off','Monitor stopped']}
function attention(){
  const items=[],bad=S.activity.filter(a=>a.status==='delivery-unconfirmed'||a.status==='partially-accepted'),drafts=S.activity.filter(a=>a.status==='draft');
  if(!S.authorized)items.push(['need','Google sign-in needed','Run google_mail_auth.py on the Mac.','settings']);
  if(S.enabled&&!S.monitor)items.push(['need','Monitor is not running','Background monitoring is enabled but the monitor is stopped.','settings']);
  if(bad.length)items.push(['need',bad.length+' send'+(bad.length>1?'s':'')+' unconfirmed','Never resent automatically. Check delivery.','activity']);
  if(drafts.length)items.push(['w',drafts.length+' draft'+(drafts.length>1?'s':'')+' to review','Saved locally; nothing was sent.','activity']);
  return items}

function pill(a,cls){return el('span','tg '+cls,a)}
function render(){
  $('connection').textContent='Connected locally';
  $('mode').textContent={off:'Off',draft:'Draft',send:'Live'}[S.mode]||S.mode;
  $('modeNote').textContent=S.mode==='send'?'Messages can be sent automatically':'No live sending';
  $('monitor').textContent=S.monitor?'Running':'Stopped';
  $('monitorNote').textContent=S.monitor?(S.enabled?'Background monitoring enabled':'Monitor running'):'Start in Settings';
  $('google').textContent=S.authorized?'Authorized':'Setup needed';
  $('monitorLine').textContent=S.monitor?'Running':'Stopped';
  if(document.activeElement!==$('modeSelect'))$('modeSelect').value=S.mode;
  $('saveMode').disabled=S.monitor;$('start').disabled=S.monitor;$('stop').disabled=!S.managed;
  const items=attention();
  $('attnCount').textContent=items.length;
  $('needs').replaceChildren(...(items.length?items.map(([lvl,t,d,go])=>{
    const n=el('div','nd '+(lvl==='w'?'w':''));n.append(el('b','',t),el('small','',d));
    const b=el('button','','Open');b.onclick=()=>navigate(go);n.append(b);return n}):[el('p','mut small','Nothing needs you right now.')]));
  $('needBadge').hidden=!items.length;$('needBadge').textContent=items.length;
  const bad=S.activity.filter(a=>a.status==='delivery-unconfirmed'||a.status==='partially-accepted').length;
  $('actBadge').hidden=!bad;$('actBadge').textContent=bad;
  const team=S.agents.map(a=>{const[st,txt]=agentState(a.name);
    const b=el('button','ag'+(a.name===agent?' sel':''));const av=el('div','av',a.name[0]);av.append(el('i',st));
    const t=el('div');t.append(el('b','',a.name+' · '+a.role),el('small','',txt));b.append(av,t);b.onclick=()=>pickAgent(a.name);return b});
  $('team').replaceChildren(...team);
  $('agents').replaceChildren(...S.agents.map(a=>{const c=el('article','card');c.append(el('b','',a.name),el('p','mut',a.role),el('small','',a.email));return c}));
  $('recent').replaceChildren(...S.activity.slice(0,5).map(a=>{const r=el('tr');r.append(el('td','',new Date(a.time).toLocaleTimeString()),el('td','',a.subject||'(No subject)'),el('td','',LABEL[a.status]||a.status));return r}));
  renderRows();renderReports();
  $('wifiUrl').textContent=S.wifi?.enabled?S.wifi.url:'Wi-Fi access has not been enabled.';
  $('telegramStatus').textContent=S.telegram?.configured?(S.telegram.enabled?'Enabled':'Disabled')+' · '+S.telegram.paired+' paired':'Token needed';
  if(document.activeElement!==$('telegramEnabled'))$('telegramEnabled').checked=!!S.telegram?.enabled;
  if($('mailboxSelect').options.length===1)$('mailboxSelect').replaceChildren(...S.mailboxes.map(m=>{const o=el('option','',m.label+(m.email?' — '+m.email:''));o.value=m.name;return o}));
  showPage()}

function renderRows(){
  const list=S.activity.filter(a=>filt==='all'||a.status===filt||a.receipt?.state===filt||(filt==='delivery-unconfirmed'&&a.receipt?.state==='failure-notice'));
  const rows=list.map(a=>{const r=el('tr');const who=el('td','',a.sender);who.append(el('small','',a.to));
    const msg=el('td');const b=el('button','',a.subject||'(No subject)');
    b.onclick=()=>detail(a.subject,a.sender+' → '+a.to+' · '+a.status+(a.receipt?' · '+a.receipt.label:'')+(a.diagnostic?' · '+a.diagnostic:''),a.body);msg.append(b,el('small','',a.kind));
    const st=el('td');st.append(pill(LABEL[a.status]||a.status,a.status==='accepted'?'':a.status==='draft'?'d':'w'));
    st.append(el('small','',a.receipt?a.receipt.label:(a.status==='draft'?'':'Confirmation not checked')));
    r.append(el('td','',new Date(a.time).toLocaleString()),who,msg,st);return r});
  if(!rows.length){const r=el('tr'),c=el('td','','No messages in this view yet.');c.colSpan=4;r.append(c);rows.push(r)}
  $('rows').replaceChildren(...rows)}

function renderReports(){
  const names=['all',...new Set(S.agents.map(a=>a.name)),'Unassigned'];
  $('reportFilters').replaceChildren(...names.map(n=>{const b=el('button',n===repFilter?'on':'',n==='all'?'All':n);b.onclick=()=>{repFilter=n;renderReports()};return b}));
  const list=S.reports.map((r,i)=>[r,i]).filter(([r])=>repFilter==='all'||r.agent===repFilter);
  $('reportList').replaceChildren(...(list.length?list.map(([r,i])=>{const b=el('button','m'+(i===repIdx?' on':''));b.append(el('b','',r.name),el('small','',r.agent));b.onclick=()=>{repIdx=i;renderReports()};return b}):[el('p','mut','No saved reports for this selection yet.')]));
  $('reportBody').textContent=repIdx!==null&&S.reports[repIdx]?S.reports[repIdx].body:'Select a report.'}

async function refresh(){try{const r=await fetch('/api/status');if(r.status===401){location.reload();return}if(!r.ok)throw 0;S=await r.json();render()}catch{$('connection').textContent='Dashboard offline'}}

/* Navigation */
function showPage(){let p=location.pathname.split('/')[1]||'overview';if(!PAGES.includes(p))p='overview';
  document.querySelectorAll('[data-page]').forEach(s=>s.hidden=s.dataset.page!==p);
  document.querySelectorAll('[data-nav]').forEach(a=>{const cur=a.dataset.nav===p;a.classList.toggle('current',cur);cur?a.setAttribute('aria-current','page'):a.removeAttribute('aria-current')});
  if(p!==page){page=p;say('');document.title=p[0].toUpperCase()+p.slice(1)+' • Onyx & Ink'}
  if(p==='inbox'&&S&&!inboxLoaded&&!inboxBusy)loadInbox(false)}
function navigate(p){history.pushState({},'','/'+p);showPage()}
document.querySelectorAll('[data-nav]').forEach(a=>a.addEventListener('click',e=>{if(e.metaKey||e.ctrlKey)return;e.preventDefault();navigate(a.dataset.nav)}));
addEventListener('popstate',showPage);

function pickAgent(n){agent=n;$('cw').textContent=n;$('draftWho').textContent=n;if(opened)$('replySender').value=n;render();loadChat()}

/* Inbox */
async function loadInbox(force){if(inboxBusy)return;inboxBusy=true;const mailbox=$('mailboxSelect').value;$('refreshInbox').disabled=true;$('inboxNotice').textContent='Loading inbox…';
  try{const d=await api('inbox',{refresh:force,mailbox});if(mailbox!==$('mailboxSelect').value)return;inboxLoaded=true;
    $('inboxList').replaceChildren(...(d.messages.length?d.messages.map(m=>{const b=el('button','m'+(m.unread?' unread':''));b.append(el('b','',m.subject||'(No subject)'),el('small','',m.from+' · '+m.date));b.onclick=()=>openMail(m.id);return b}):[el('p','mut','Your inbox is empty.')]));
    const unread=d.messages.filter(m=>m.unread).length;$('unreadBadge').hidden=!unread;$('unreadBadge').textContent=unread;$('inboxNotice').textContent=''}
  catch(e){$('inboxNotice').textContent=e.message}finally{inboxBusy=false;$('refreshInbox').disabled=false}}
async function openMail(id){$('inboxNotice').textContent='Opening message…';
  try{opened=await api('message',{id});replyId=crypto.randomUUID();
    $('mailSubject').textContent=opened.subject||'(No subject)';$('mailMeta').textContent=opened.from+' → '+opened.to+' · '+opened.date;$('mailBody').textContent=opened.body;
    $('mailAttachments').textContent=opened.attachments.length?'Attachments: '+opened.attachments.join(', ')+' (open in Gmail to view)':'';
    $('replyTo').textContent='→ '+opened.replyTo+' · personal Gmail BCC’d';
    const box=$('mailboxSelect').value;$('replySender').value=['Owner','Shared','Morgan','Avery','Jordan','Cameron'].includes(box)?box:(S.agents.some(a=>a.name===agent)?agent:'Owner');
    $('replyBody').value='';$('replyNotice').textContent='';$('sendReply').disabled=false;$('sendReply').textContent=S.mode==='draft'?'Save reply draft':'Send reply';
    $('reader').hidden=false;$('inboxNotice').textContent=''}catch(e){$('inboxNotice').textContent=e.message}}
on('refreshInbox','click',()=>loadInbox(true));
on('mailboxSelect','change',()=>{inboxLoaded=false;$('inboxList').replaceChildren(el('p','mut','Loading selected inbox…'));loadInbox(false)});
on('sendReply','click',async()=>{if(!opened||!$('replyBody').value.trim())return;$('sendReply').disabled=true;
  try{await refresh();if(S.mode==='off')throw Error('Email is off. Select Draft or Live in Settings.');
    if(S.mode==='send'&&!confirm('Send this reply to '+opened.replyTo+' now? Your personal Gmail will be BCC’d.')){$('sendReply').disabled=false;return}
    const r=await api('reply',{id:opened.id,body:$('replyBody').value,sender:$('replySender').value,requestId:replyId});$('replyNotice').textContent=r.result;await refresh()}
  catch(e){$('replyNotice').textContent=e.message+' If delivery is uncertain, do not resubmit as a new reply.';$('sendReply').disabled=false}});
on('askDraft','click',()=>{if(!opened)return;askAgent('Draft a reply to this email. Subject: '+opened.subject+'\n\n'+opened.body.slice(0,1500))});

/* Compose */
on('composeMail','click',()=>{composeId=crypto.randomUUID();const b=$('mailboxSelect').value;$('composeSender').value=['Owner','Shared','Morgan','Avery','Jordan','Cameron'].includes(b)?b:'Owner';
  ['composeTo','composeSubject','composeBody'].forEach(i=>$(i).value='');$('composeNotice').textContent='';$('sendCompose').disabled=false;$('sendCompose').textContent=S.mode==='draft'?'Save draft':'Send email';$('composeDetail').showModal()});
on('composeClose','click',()=>$('composeDetail').close());
on('sendCompose','click',async()=>{if(!$('composeTo').checkValidity()||!$('composeSubject').value.trim()||!$('composeBody').value.trim()){$('composeNotice').textContent='Enter a valid recipient, subject, and message.';return}
  if(S.mode==='send'&&!confirm('Send this new email to '+$('composeTo').value+'?'))return;$('sendCompose').disabled=true;
  try{const r=await api('compose',{sender:$('composeSender').value,to:$('composeTo').value,subject:$('composeSubject').value,body:$('composeBody').value,requestId:composeId});$('composeNotice').textContent=r.result;await refresh()}
  catch(e){$('composeNotice').textContent=e.message+' If delivery is uncertain, do not recreate and resend.';$('sendCompose').disabled=false}});

/* Activity / settings */
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filt=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('on',x===b));renderRows()}));
on('checkDelivery','click',async()=>{$('checkDelivery').disabled=true;say('Checking Gmail for sending records, replies, and failure notices…');
  try{say((await api('delivery',{})).message);await refresh()}catch(e){say(e.message)}finally{$('checkDelivery').disabled=false}});
const act=async(p,d)=>{try{const r=await api(p,d);say(r.error||'Settings updated.');await refresh()}catch(e){say(e.message)}};
on('saveMode','click',()=>{const mode=$('modeSelect').value;if(mode==='send'&&!confirm('Enable live sending? The running monitor will send agent replies and forward outside emails to the CEO.'))return;act('mode',{mode})});
on('start','click',()=>{if(S.mode==='send'&&!confirm('Start the monitor with live email sending enabled?'))return;act('start')});
on('stop','click',()=>act('stop'));
on('close','click',()=>$('detail').close());
on('showWifiPassword','click',async()=>{try{$('wifiPassword').textContent=(await api('wifi-password',{})).password}catch(e){$('wifiPassword').textContent=e.message}});
on('saveTelegram','click',async()=>{try{await api('telegram',{token:$('telegramToken').value,enabled:$('telegramEnabled').checked});$('telegramToken').value='';await refresh();say('Telegram settings saved.')}catch(e){say(e.message)}});
on('pairTelegram','click',async()=>{try{const r=await api('telegram-pair',{});$('telegramPairCode').textContent='In your bot’s private chat, send /pair '+r.code+' within 10 minutes. This grants access to the shared staff conversations.'}catch(e){$('telegramPairCode').textContent=e.message}});

/* Chat (shared with Telegram through the same history table) */
async function loadChat(){if(!S||chatBusy)return;const who=agent;
  try{const r=await api('chat-history',{agent:who});if(who!==agent)return;
    $('chatHistory').replaceChildren(...(r.messages.length?r.messages.map(m=>{const b=el('article','b '+m.role);b.append(el('p','',m.body),el('small','',(m.role==='user'?'You':who)+' · '+m.source+' · '+new Date(m.created.replace(' ','T')+'Z').toLocaleTimeString()));return b}):[el('p','mut small','Start a conversation with '+who+'.')]));
    $('chatHistory').scrollTop=1e6;$('chatNotice').textContent=''}catch(e){$('chatNotice').textContent=e.message}}
async function askAgent(text){const body=text.trim();if(!body||chatBusy)return;chatBusy=true;$('sendChat').disabled=true;$('chatNotice').textContent=agent+' is thinking…';
  try{await api('chat',{agent,body:body.slice(0,4000),requestId:crypto.randomUUID()});$('chatBody').value=''}catch(e){$('chatNotice').textContent=e.message}
  finally{chatBusy=false;$('sendChat').disabled=false;await loadChat()}}
on('chatForm','submit',e=>{e.preventDefault();askAgent($('chatBody').value)});
[['Summarize today','Summarize what needs my attention today based on our conversation.'],['What’s blocked?','What is blocked or waiting on my decision?']].forEach(([l,t])=>{const b=el('button','',l);b.type='button';b.onclick=()=>askAgent(t);$('chips').append(b)});

refresh().then(()=>{$('cw').textContent=agent;loadChat()});setInterval(refresh,5000);setInterval(()=>{if(!chatBusy)loadChat()},10000);
})();
