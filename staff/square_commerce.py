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

    def locations(self):
        return self._request('GET', '/locations').get('locations', [])

    def find_variation(self, item_name):
        item_id = self.find_item_by_name(item_name)
        if not item_id:
            raise ValueError('No Square catalog item named %s exists.' % item_name)
        item = self._request('GET', '/catalog/object/' + item_id).get('object', {})
        variations = item.get('item_data', {}).get('variations', [])
        if not variations:
            raise ValueError('Square catalog item %s has no variation to stock.' % item_name)
        variation = variations[0]
        return {'item_id': item_id, 'variation_id': variation.get('id', ''),
                'variation_name': variation.get('item_variation_data', {}).get('name', '')}

    def get_inventory_count(self, variation_id, location_id):
        data = self._request('GET', '/inventory/counts?catalog_object_id=' + variation_id +
                             '&location_ids=' + location_id)
        total = 0
        for count in data.get('counts', []):
            if count.get('state') == 'IN_STOCK':
                try:
                    total += int(count.get('quantity', 0))
                except (TypeError, ValueError):
                    pass
        return total

    def set_inventory(self, item_name, quantity, location_id=''):
        """Set exact production stock via a physical count. Production only.

        The sandbox token can never set production stock, so this refuses
        outright unless SQUARE_ENVIRONMENT is production — a sandbox
        count must never be mistaken for a production one. Quantities
        are never invented by the caller: the agent action passes only
        James-approved numbers.
        """
        if self.mode != 'production':
            raise RuntimeError('Square is in sandbox mode. Production stock needs a production '
                               'access token and SQUARE_ENVIRONMENT=production; refusing a sandbox count.')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0 or quantity > 100000:
            raise ValueError('Use a whole stock quantity from 0 to 100000.')
        variation = self.find_variation(item_name)
        if not location_id:
            places = self.locations()
            if not places:
                raise ValueError('No Square location exists to hold stock.')
            if len(places) > 1:
                raise ValueError('More than one Square location exists; pass location_id explicitly.')
            location_id = places[0].get('id', '')
        before = self.get_inventory_count(variation['variation_id'], location_id)
        from datetime import datetime, timezone
        occurred = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        self._request('POST', '/inventory/changes', json={
            'idempotency_key': str(uuid.uuid4()),
            'changes': [{'type': 'PHYSICAL_COUNT', 'physical_count': {
                'catalog_object_id': variation['variation_id'],
                'location_id': location_id, 'quantity': str(quantity),
                'occurred_at': occurred}}]})
        after = self.get_inventory_count(variation['variation_id'], location_id)
        return {'item': item_name, 'variation_id': variation['variation_id'],
                'location_id': location_id, 'before': before, 'quantity': quantity,
                'after': after, 'environment': self.mode}