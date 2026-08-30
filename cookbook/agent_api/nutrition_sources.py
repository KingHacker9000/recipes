import os
from decimal import Decimal, InvalidOperation

import requests
from django.core.cache import cache


FDC_BASE_URL = 'https://api.nal.usda.gov/fdc/v1'
FDC_SEARCH_TTL = 12 * 60 * 60
FDC_DETAIL_TTL = 7 * 24 * 60 * 60
FDC_DATA_TYPES = ('Foundation', 'SR Legacy', 'Survey (FNDDS)', 'Branded')


class FoodDataCentralError(RuntimeError):
    pass


class FoodDataCentralNotFound(FoodDataCentralError):
    pass


def _api_key():
    key = str(os.environ.get('USDA_FDC_API_KEY') or '').strip()
    if not key:
        raise FoodDataCentralError('USDA_FDC_API_KEY is not configured on the server.')
    return key


def _request(path, *, params=None, json=None):
    query = dict(params or {})
    query['api_key'] = _api_key()
    try:
        if json is None:
            response = requests.get(f'{FDC_BASE_URL}/{path.lstrip("/")}', params=query, timeout=12)
        else:
            response = requests.post(f'{FDC_BASE_URL}/{path.lstrip("/")}', params=query, json=json, timeout=12)
    except requests.RequestException as exc:
        raise FoodDataCentralError(f'FoodData Central request failed: {exc.__class__.__name__}.')
    if response.status_code == 404:
        raise FoodDataCentralNotFound('FoodData Central record was not found.')
    if response.status_code >= 400:
        raise FoodDataCentralError(f'FoodData Central returned HTTP {response.status_code}.')
    try:
        return response.json()
    except ValueError:
        raise FoodDataCentralError('FoodData Central returned invalid JSON.')


def search_foods(query, *, page_size=10, data_types=None):
    query = str(query or '').strip()
    if len(query) < 2:
        raise FoodDataCentralError('query must contain at least two characters.')
    try:
        page_size = min(max(int(page_size), 1), 25)
    except (TypeError, ValueError):
        raise FoodDataCentralError('limit must be an integer between 1 and 25.')
    data_types = list(data_types or FDC_DATA_TYPES)
    if not data_types or any(value not in FDC_DATA_TYPES for value in data_types):
        raise FoodDataCentralError('Unsupported FoodData Central data type.')

    cache_key = f'agent:fdc:search:{query.lower()}:{page_size}:{"|".join(data_types)}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    payload = _request('foods/search', json={
        'query': query,
        'pageSize': page_size,
        'dataType': data_types,
    })
    foods = []
    for item in payload.get('foods') or []:
        foods.append({
            'fdc_id': item.get('fdcId'),
            'description': item.get('description') or '',
            'data_type': item.get('dataType') or '',
            'brand_owner': item.get('brandOwner') or '',
            'brand_name': item.get('brandName') or '',
            'gtin_upc': item.get('gtinUpc') or '',
            'food_category': item.get('foodCategory') or '',
            'published_date': item.get('publishedDate') or '',
        })
    result = {'query': query, 'foods': foods}
    cache.set(cache_key, result, timeout=FDC_SEARCH_TTL)
    return result


def food_details(fdc_id, *, force_refresh=False):
    try:
        fdc_id = int(fdc_id)
    except (TypeError, ValueError):
        raise FoodDataCentralError('fdc_id must be an integer.')
    if fdc_id <= 0:
        raise FoodDataCentralError('fdc_id must be positive.')
    cache_key = f'agent:fdc:food:{fdc_id}'
    if not force_refresh:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    payload = _request(f'food/{fdc_id}')
    cache.set(cache_key, payload, timeout=FDC_DETAIL_TTL)
    return payload


def invalidate_food_cache(fdc_id):
    cache.delete(f'agent:fdc:food:{int(fdc_id)}')


def _amount(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _nutrient_index(payload):
    index = {}
    for item in payload.get('foodNutrients') or []:
        nutrient = item.get('nutrient') or {}
        name = str(nutrient.get('name') or item.get('nutrientName') or '').strip().lower()
        unit = str(nutrient.get('unitName') or item.get('unitName') or '').strip().lower()
        amount = _amount(item.get('amount') if 'amount' in item else item.get('value'))
        if name and amount is not None:
            index.setdefault((name, unit), amount)
    return index


def _first(index, candidates):
    for name, unit in candidates:
        value = index.get((name.lower(), unit.lower()))
        if value is not None:
            return value
    return None


def nutrition_profile_from_fdc(payload):
    """Normalize verified FDC nutrients into the Agent API's per-100g contract."""
    index = _nutrient_index(payload)
    values = {
        'calories': _first(index, [
            ('Energy', 'kcal'),
            ('Energy (Atwater General Factors)', 'kcal'),
            ('Energy (Atwater Specific Factors)', 'kcal'),
        ]),
        'protein_g': _first(index, [('Protein', 'g')]),
        'carbohydrate_g': _first(index, [('Carbohydrate, by difference', 'g')]),
        'fat_g': _first(index, [('Total lipid (fat)', 'g')]),
        'fiber_g': _first(index, [('Fiber, total dietary', 'g')]),
        'sugar_g': _first(index, [('Sugars, Total', 'g'), ('Total Sugars', 'g')]),
        'sodium_mg': _first(index, [('Sodium, Na', 'mg')]),
    }
    return {
        'fdc_id': payload.get('fdcId'),
        'description': payload.get('description') or '',
        'data_type': payload.get('dataType') or '',
        'brand_owner': payload.get('brandOwner') or '',
        'brand_name': payload.get('brandName') or '',
        'gtin_upc': payload.get('gtinUpc') or '',
        'basis_amount': Decimal('100'),
        'basis_unit': 'g',
        **values,
    }
