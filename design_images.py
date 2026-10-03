"""Cloudflare-only generation; no paid fallback and a conservative local daily cap."""
import base64
import io
import re
import sqlite3
from contextlib import closing
from datetime import datetime,timezone
import requests
from dotenv import dotenv_values

MODELS={'schnell':'@cf/black-forest-labs/flux-1-schnell','klein':'@cf/black-forest-labs/flux-2-klein-4b'}

def generate(root,prompt,model='schnell',reference=None):
    from PIL import Image
    cfg=dotenv_values(root/'.env');account=cfg.get('CLOUDFLARE_ACCOUNT_ID','');token=cfg.get('CLOUDFLARE_API_TOKEN','')
    if model not in MODELS:raise ValueError('Choose schnell or klein.')
    if cfg.get('CLOUDFLARE_FREE_PLAN_CONFIRMED')!='true' or not re.fullmatch('[a-fA-F0-9]{32}',account) or not token:raise RuntimeError('Add Cloudflare credentials and confirm the Free plan in Settings.')
    if reference and model!='klein':raise ValueError('Reference editing requires klein.')
    day=datetime.now(timezone.utc).date().isoformat();path=root/'work'/'image-usage.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,count INTEGER)');db.commit();db.execute('BEGIN IMMEDIATE')
        count=db.execute('SELECT count FROM usage WHERE day=?',(day,)).fetchone()
        if count and count[0]>=50:raise RuntimeError('The local 50-image daily limit has been reached. It resets at midnight UTC.')
        db.execute('INSERT INTO usage VALUES (?,1) ON CONFLICT(day) DO UPDATE SET count=count+1',(day,));db.commit()
    url='https://api.cloudflare.com/client/v4/accounts/'+account+'/ai/run/'+MODELS[model]
    kwargs={'json':{'prompt':prompt,'steps':4}} if model=='schnell' else {'files':{'prompt':(None,prompt),'width':(None,'1024'),'height':(None,'1024')}}
    if reference:
        with Image.open(reference) as source:
            source.thumbnail((511,511));buffer=io.BytesIO();source.convert('RGB').save(buffer,'PNG')
        kwargs['files']['input_image_0']=('reference.png',buffer.getvalue(),'image/png')
    # No retries: a timed-out generation may already have consumed provider quota.
    r=requests.post(url,headers={'Authorization':'Bearer '+token},timeout=90,**kwargs)
    if not r.ok:raise RuntimeError('Cloudflare generation failed. Check token permissions, model availability, and daily quota.')
    if r.headers.get('Content-Type','').startswith('image/'):data=r.content
    else:
        result=r.json()
        if not result.get('success',True):raise RuntimeError('Cloudflare rejected generation.')
        data=base64.b64decode(result.get('result',{}).get('image',''),validate=True)
    if len(data)>15_000_000:raise RuntimeError('Generated image exceeds the file limit.')
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>16_000_000:raise RuntimeError('Generated image dimensions exceed the limit.')
        image.load();output=io.BytesIO();image.convert('RGB').save(output,format='PNG');return output.getvalue()
