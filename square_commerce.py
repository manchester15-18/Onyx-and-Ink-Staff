"""Square Catalog connection for Jordan's approved product publishing."""
import json
import re
import uuid
from pathlib import Path

import requests
from dotenv import dotenv_values


class SquareCommerce:
    def __init__(self, root):
        self.root = Path(root)
        self.cfg = dotenv_values(self.root / '.env')
        self.token = self.cfg.get('SQUARE_ACCESS_TOKEN', '').strip()
        self.mode = self.cfg.get('SQUARE_ENVIRONMENT', 'sandbox').strip().lower()
        if self.mode not in ('sandbox', 'production'):
            self.mode = 'sandbox'
        self.base = 'https://connect.squareup.com/v2' if self.mode == 'production' else 'https://connect.squareupsandbox.com/v2'

    @property
    def configured(self):
        return bool(self.token)

    def _request(self, method, path, **kwargs):
        if not self.configured:
            raise RuntimeError('Connect Square in Settings before publishing products.')
        headers = {'Authorization': 'Bearer ' + self.token, 'Square-Version': '2026-09-16', 'User-Agent': 'OnyxAndInkStaff/1.0'}
        headers.update(kwargs.pop('headers', {}))
        response = requests.request(method, self.base + path, headers=headers, timeout=30, **kwargs)
        if not response.ok:
            try:
                detail = response.json().get('errors', [{}])[0].get('detail', '')
            except (ValueError, IndexError, TypeError):
                detail = ''
            raise RuntimeError('Square request failed' + (': ' + detail if detail else '.') )
        return response.json() if response.content else {}

    def check(self):
        data = self._request('GET', '/locations')
        locations = data.get('locations', [])
        return {'environment': self.mode, 'locations': len(locations), 'message': 'Square ' + self.mode + ' connection verified.'}

    def find_item_by_name(self, name):
        data=self._request('GET','/catalog/list?types=ITEM')
        for item in data.get('objects',[]):
            if item.get('type')=='ITEM' and item.get('item_data',{}).get('name')==name:
                return item.get('id','')
        return ''
    def publish_product(self, product, image_path=None):
        amount = int(round(float(product['price']) * 100))
        item_id = product.get('square_item_id') or self.find_item_by_name(product['title'])
        if item_id:
            item=self._request('GET','/catalog/object/'+item_id).get('object',{})
            if not item:raise RuntimeError('Square catalog item could not be retrieved for update.')
            details=item.setdefault('item_data',{})
            details['name']=product['title'];details['description']=product['description']
            variations=details.get('variations',[])
            if variations:
                variation=variations[0].setdefault('item_variation_data',{})
                variation['price_money']={'amount':amount,'currency':'USD'}
            else:
                details['variations']=[{'id':'#regular-'+product['id'],'type':'ITEM_VARIATION','item_variation_data':{
                    'name':'Regular','pricing_type':'FIXED_PRICING','price_money':{'amount':amount,'currency':'USD'}}}]
        else:
            item = {'id': '#' + product['id'], 'type': 'ITEM', 'item_data': {
                'name': product['title'], 'description': product['description'],
                'variations': [{'id': '#regular-' + product['id'], 'type': 'ITEM_VARIATION', 'item_variation_data': {
                    'name': 'Regular', 'pricing_type': 'FIXED_PRICING', 'price_money': {'amount': amount, 'currency': 'USD'}}}]}}
        data = self._request('POST', '/catalog/object', json={'idempotency_key': str(uuid.uuid4()), 'object': item})
        square_id = data.get('catalog_object', {}).get('id', '')
        if not square_id:
            raise RuntimeError('Square did not return a catalog item ID.')
        if image_path:
            with Path(image_path).open('rb') as image:
                payload=json.dumps({'idempotency_key': str(uuid.uuid4()), 'object_id': square_id,
                                    'image': {'id': '#image-' + uuid.uuid4().hex[:12], 'type': 'IMAGE', 'image_data': {'caption': product['title']}}})
                self._request('POST', '/catalog/images', files={
                    'file': (Path(image_path).name, image, 'image/png'),
                    'request': (None, payload)})
        return {'square_item_id': square_id, 'environment': self.mode}