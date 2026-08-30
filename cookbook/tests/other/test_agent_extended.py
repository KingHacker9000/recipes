from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import auth
from django_scopes import scopes_disabled

from cookbook.agent_api.household import accessible_recipe_queryset
from cookbook.agent_api.nutrition_sources import FoodDataCentralError, nutrition_profile_from_fdc, search_foods
from cookbook.agent_api.recommendations import _macro_check
from cookbook.agent_api.scaling import _round_to_step
from cookbook.models import Recipe
from cookbook.views.agent_pantry import _snapshot_has_unresolved
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


def test_fdc_branded_liquid_preserves_100ml_basis():
    result = nutrition_profile_from_fdc({
        'fdcId': 222,
        'dataType': 'Branded',
        'servingSizeUnit': 'ml',
        'foodNutrients': [
            {'nutrient': {'name': 'Energy', 'unitName': 'kcal'}, 'amount': 42},
        ],
    })
    assert result['basis_amount'] == Decimal('100')
    assert result['basis_unit'] == 'ml'
    assert result['calories'] == Decimal('42')


def test_fdc_branded_ambiguous_basis_is_rejected():
    with pytest.raises(FoodDataCentralError, match='ambiguous serving-size basis'):
        nutrition_profile_from_fdc({
            'fdcId': 333,
            'dataType': 'Branded',
            'servingSizeUnit': 'serving',
            'foodNutrients': [],
        })


def test_fdc_parser_does_not_invent_missing_nutrients():
    result = nutrition_profile_from_fdc({'fdcId': 1, 'foodNutrients': []})
    assert result['calories'] is None
    assert result['protein_g'] is None
    assert result['fat_g'] is None


def test_fdc_search_rejects_bad_limit_before_network():
    with pytest.raises(FoodDataCentralError, match='limit must be an integer'):
        search_foods('chicken', page_size='many')


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


def test_snapshot_with_unresolved_observations_is_blocked():
    proposal = SimpleNamespace(
        payload={'mode': 'snapshot'},
        preview={'summary': {'unresolved_actions': 1}},
    )
    assert _snapshot_has_unresolved(proposal) is True


def test_augment_with_unresolved_observations_is_not_snapshot_blocked():
    proposal = SimpleNamespace(
        payload={'mode': 'augment'},
        preview={'summary': {'unresolved_actions': 3}},
    )
    assert _snapshot_has_unresolved(proposal) is False


def test_private_recipe_queryset_hides_unshared_recipe(space_1, u1_s1, u2_s1):
    viewer = auth.get_user(u1_s1)
    owner = auth.get_user(u2_s1)
    with scopes_disabled():
        private_recipe = Recipe.objects.create(
            name='Private meal-plan safety test',
            servings=1,
            private=True,
            created_by=owner,
            space=space_1,
        )
        request = SimpleNamespace(user=viewer, space=space_1)
        assert accessible_recipe_queryset(request).filter(pk=private_recipe.pk).exists() is False
        private_recipe.shared.add(viewer)
        assert accessible_recipe_queryset(request).filter(pk=private_recipe.pk).exists() is True


def test_mcp_registry_has_no_generic_http_or_sql_escape_hatch():
    forbidden = {'fetch', 'http', 'request', 'sql', 'query_database', 'execute_sql'}
    assert not forbidden.intersection(TOOLS)
    for expected in (
        'recipes_search', 'recipe_create', 'recipe_update', 'recipe_clone',
        'pantry_locations', 'pantry_reconcile_preview', 'proposal_get',
        'recipe_save_variant', 'nutrition_profile_create', 'shopping_entry_delete',
        'meal_plan_add', 'audit_events',
    ):
        assert expected in TOOLS


@pytest.mark.asyncio
async def test_mcp_client_refuses_non_agent_api_paths_before_network():
    client = TandoorAgentClient(base_url='https://cook.example.test', token='secret')
    with pytest.raises(TandoorAgentClientError, match='Refusing non-Agent-API path'):
        await client.request('GET', 'https://evil.example/api')
