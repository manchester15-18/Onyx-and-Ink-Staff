"""Bounded local product catalog used by Jordan before a commerce platform is connected."""
import json
import re
import sqlite3
import uuid
from pathlib import Path


class Storefront:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'work'/'storefront.sqlite3';self.path.parent.mkdir(exist_ok=True)
    def db(self):
        db=sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,price TEXT NOT NULL,image_file_id TEXT,image_name TEXT,square_item_id TEXT,status TEXT NOT NULL,updated TEXT DEFAULT CURRENT_TIMESTAMP)')
        try: db.execute('ALTER TABLE products ADD COLUMN square_item_id TEXT')
        except sqlite3.OperationalError: pass
        return db
    @staticmethod
    def clean_text(value,key,maximum):
        if not isinstance(value,str) or not value.strip() or len(value)>maximum:raise ValueError('Provide '+key+' within '+str(maximum)+' characters.')
        return value.strip()
    @staticmethod
    def price(value):
        value=str(value).strip()
        if not re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?',value) or float(value)<0:raise ValueError('Use a non-negative price with up to two decimals.')
        return format(float(value),'.2f')
    def list(self,query=''):
        query=str(query).strip().lower()
        with self.db() as db:
            rows=db.execute('SELECT id,title,description,price,image_file_id,image_name,square_item_id,status,updated FROM products ORDER BY updated DESC LIMIT 50').fetchall()
        keys=('id','title','description','price','image_file_id','image_name','square_item_id','status','updated')
        items=[dict(zip(keys,row)) for row in rows]
        return [item for item in items if not query or query in (item['id']+' '+item['title']+' '+item['description']).lower()]
    def set_square_item(self, identifier, square_item_id):
        with self.db() as db:
            db.execute('UPDATE products SET square_item_id=?,status=?,updated=CURRENT_TIMESTAMP WHERE id=?',(square_item_id,'published',identifier));db.commit()
    def update(self,values,image=None):
        identifier=str(values.get('id','')).strip()
        title=self.clean_text(values.get('title'),'title',160)
        description=self.clean_text(values.get('description'),'description',12000)
        price=self.price(values.get('price'))
        status=str(values.get('status','draft')).lower()
        if status not in ('draft','ready','published'):raise ValueError('Use draft, ready, or published status.')
        if not identifier:identifier='prd_'+uuid.uuid4().hex[:12]
        if not re.fullmatch(r'[A-Za-z0-9_-]{3,80}',identifier):raise ValueError('Product ID may contain letters, numbers, underscores, and hyphens.')
        image_id,image_name=(image or ('',''))
        square_item_id=None
        with self.db() as db:
            prior=db.execute('SELECT image_file_id,image_name,square_item_id FROM products WHERE id=?',(identifier,)).fetchone()
            if prior: square_item_id=prior[2]
            if prior and not image_id:image_id,image_name=prior[:2]
            db.execute('INSERT INTO products(id,title,description,price,image_file_id,image_name,status) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,description=excluded.description,price=excluded.price,image_file_id=excluded.image_file_id,image_name=excluded.image_name,status=excluded.status,updated=CURRENT_TIMESTAMP',(identifier,title,description,price,image_id,image_name,status));db.commit()
        return {'id':identifier,'title':title,'description':description,'price':price,'image_file_id':image_id or None,'image_name':image_name or None,'square_item_id':square_item_id,'status':status,'catalog':'local'}
