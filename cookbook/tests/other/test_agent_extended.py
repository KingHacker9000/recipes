from decimal import Decimal

import pytest

from cookbook.agent_api.nutrition_sources import nutrition_profile_from_fdc
from cookbook.agent_api.recommendations import _macro_check
from cookbook.agent_api.scaling import _round_to_step
from tandoor_mcp.client import TandoorAgentClient, TandoorAgentClientError
from tandoor_mcp.server import TOOLS


def test_fdc_profile_parser_uses_verified_per_100g_contract():
    payload = {
        'fdcId': 12345,
        'description': 'Chicken breast, raw',
        'dataType': 'Foundation',
        'foodNutrients': [
            {'nutrient': {'name': 'Energy', 'unitName': 'kcal'}, 'amount': 120},
            {'nutrient': {'name': 'Protein', 'unitName': 'g'}, 'amount': 22.5},
            {'nutrient': {'name': 'Carbohydrate, by difference', 'unitName': 'g'}, 'amount': 0},
            {'nutrient': {'name': 'Total lipid (fat)', 'unitName': 'g'}, 'amount': 2.6},
            {'nutrient': {'name': 'Sodium, Na', 'unitName': 'mg'}, 'amount': 45},
        ],
    }
    result = nutrition_profile_from_fdc(payload)
    assert result['fdc_id'] == 12345
    assert result['basis_amount'] == Decimal('100')
    assert result['basis_unit'] == 'g'
    assert result['calories'] == Decimal('120')
    assert result['protein_g'] == Decimal('22.5')
    assert result['carbohydrate_g'] == Decimal('0')
    assert result['fat_g'] == Decimal('2.6')
    assert result['sodium_mg'] == Decimal('45')


def test_fdc_parser_does_not_invent_missing_nutrients():
    result = nutrition_profile_from_fdc({'fdcId': 1, 'foodNutrients': []})
    assert result['calories'] is None
    assert result['protein_g'] is None
    assert result['fat_g'] is None


@pytest.mark.parametrize(
    ('value', 'step', 'mode', 'expected'),
    [
        ('2.4', '1', 'nearest', Decimal('2')),
        ('2.6', '1', 'nearest', Decimal('3')),
        ('2.1', '1', 'up', Decimal('3')),
        ('2.9', '1', 'down', Decimal('2')),
        ('1.24', '0.5', 'nearest', Decimal('1.0')),
        ('1.26', '0.5', 'nearest', Decimal('1.5')),
    ],
)
def test_practical_count_rounding_is_explicit(value, step, mode, expected):
    assert _round_to_step(Decimal(value), Decimal(step), mode) == expected


def test_recommendation_macro_fit_requires_full_coverage():
    analysis = {
        'per_serving': {'calories': 450, 'protein_g': 55},
        'coverage': {'field_coverage': {'calories': 1, 'protein_g': 0.8}},
    }
    result = _macro_check(analysis, calories_max=Decimal('500'), protein_min_g=Decimal('50'))
    assert result['checks'][0]['satisfied'] is True
    assert result['checks'][1]['verifiable'] is False
    assert result['all_verifiable'] is False
    assert result['all_satisfied'] is False


def test_mcp_registry_has_no_generic_http_or_sql_escape_hatch():
    forbidden = {'fetch', 'http', 'request', 'sql', 'query_database', 'execute_sql'}
    assert not forbidden.intersection(TOOLS)
    assert 'recipes_search' in TOOLS
    assert 'pantry_reconcile_preview' in TOOLS
    assert 'recipe_save_variant' in TOOLS
    assert 'nutrition_profile_create' in TOOLS


@pytest.mark.asyncio
async def test_mcp_client_refuses_non_agent_api_paths_before_network():
    client = TandoorAgentClient(base_url='https://cook.example.test', token='secret')
    with pytest.raises(TandoorAgentClientError, match='Refusing non-Agent-API path'):
        await client.request('GET', 'https://evil.example/api')
