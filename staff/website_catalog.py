"""Import the public Squarespace product cards into Square Sandbox or production."""
import html
import json
import tempfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def collect(url, limit=50):
    response=requests.get(url,timeout=30,headers={'User-Agent':'OnyxAndInkStaff/1.0'})
    response.raise_for_status()
    soup=BeautifulSoup(response.text,'html.parser')
    products=[];seen=set()
    for card in soup.select('[data-product]'):
        try:item=json.loads(html.unescape(card.get('data-product','')))
        except (ValueError,TypeError):continue
        source_id=str(item.get('id','')).strip()
        price=item.get('price',{}).get('value') if isinstance(item.get('price'),dict) else None
        title=str(item.get('title','')).strip()
        if not source_id or not title or price is None or source_id in seen:continue
        seen.add(source_id)
        description=BeautifulSoup(str(item.get('description','')),'html.parser').get_text(' ',strip=True)
        images=item.get('images') if isinstance(item.get('images'),list) else []
        image_url=next((str(image.get('assetUrl','')) for image in images if isinstance(image,dict) and str(image.get('assetUrl','')).startswith('https://')),'')
        products.append({'id':'sq_'+source_id,'title':title[:160],'description':description or 'Product details imported from the current storefront.',
                         'price':str(price),'image_url':image_url,'source_url':str(item.get('fullUrl',''))})
        if len(products)>=limit:break
    return products


def download_image(url):
    if not url:return None
    response=requests.get(url,timeout=30,headers={'User-Agent':'OnyxAndInkStaff/1.0'})
    response.raise_for_status()
    from PIL import Image
    from io import BytesIO
    image=Image.open(BytesIO(response.content)).convert('RGB')
    handle=tempfile.NamedTemporaryFile(suffix='.png',delete=False)
    image.save(handle,format='PNG');handle.close()
    return Path(handle.name)