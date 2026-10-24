"""Dashboard-only email visibility and explicitly selected attachment uploads."""
import base64
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

class InboxState:
    def __init__(self,root):self.root=Path(root)
    def db(self):
        path=self.root/'work'/'inbox-view.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE IF NOT EXISTS hidden(id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE IF NOT EXISTS visible(id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,name TEXT,path TEXT,size INTEGER)')
        return db
    def days(self):
        with closing(self.db()) as db:row=db.execute("SELECT value FROM settings WHERE key='days'").fetchone()
        return row[0] if row else 30
    def configure(self,days):
        if type(days) is not int or days not in (0,7,30,90):raise ValueError('Choose a supported dashboard cleanup period.')
        with closing(self.db()) as db:db.execute("INSERT INTO settings VALUES ('days',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(days,));db.commit()
    def hidden(self):
        with closing(self.db()) as db:return {r[0] for r in db.execute('SELECT id FROM hidden')}
    def visible(self):
        with closing(self.db()) as db:return {r[0] for r in db.execute('SELECT id FROM visible')}
    def hide(self,identifier,hide=True):
        if not re.fullmatch('[a-fA-F0-9]+',identifier):raise ValueError('Invalid message.')
        with closing(self.db()) as db:
            if hide:
                db.execute('INSERT OR IGNORE INTO hidden VALUES (?)',(identifier,));db.execute('DELETE FROM visible WHERE id=?',(identifier,))
            else:
                db.execute('DELETE FROM hidden WHERE id=?',(identifier,));db.execute('INSERT OR IGNORE INTO visible VALUES (?)',(identifier,))
            db.commit()
    def upload(self,name,encoded):
        name=Path(str(name).replace('\\','/')).name
        name=re.sub(r'[\x00-\x1f\x7f]','',name)[:140]
        if not name or name in ('.','..') or not isinstance(encoded,str) or len(encoded)>14_000_000:raise ValueError('Choose a file up to 10 MB.')
        data=base64.b64decode(encoded,validate=True)
        if not data or len(data)>10_000_000:raise ValueError('Choose a file up to 10 MB.')
        identifier=uuid.uuid4().hex;folder=self.root/'work'/'uploads';folder.mkdir(parents=True,exist_ok=True)
        path=folder/(identifier+'--'+name);path.write_bytes(data);path.chmod(0o600)
        with closing(self.db()) as db:db.execute('INSERT INTO uploads VALUES (?,?,?,?)',(identifier,name,path.name,len(data)));db.commit()
        return {'id':identifier,'name':name,'size':len(data)}
    def attachment_paths(self,ids):
        if not isinstance(ids,list) or len(ids)>5 or any(not isinstance(i,str) or not re.fullmatch('[a-f0-9]{32}',i) for i in ids):raise ValueError('Choose up to five uploaded attachments.')
        paths=[];total=0;folder=(self.root/'work'/'uploads').resolve()
        with closing(self.db()) as db:
            for identifier in ids:
                row=db.execute('SELECT path,size FROM uploads WHERE id=?',(identifier,)).fetchone()
                if not row:raise ValueError('Attachment upload not found.')
                path=(folder/row[0]).resolve()
                if not path.is_relative_to(folder) or not path.is_file():raise ValueError('Invalid attachment.')
                total+=path.stat().st_size;paths.append(path)
        if total>15_000_000:raise ValueError('Combined attachments must be under 15 MB.')
        return paths
