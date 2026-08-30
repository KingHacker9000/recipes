from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import auth
from django.db import IntegrityError
from django_scopes import scopes_disabled

from cookbook.agent_api.audit import record_agent_event
from cookbook.agent_api.models import AgentAuditEvent
from cookbook.agent_api.recipes import (
    AgentRecipeInputError,
    evaluate_macro_constraints,
    per_serving_from_analysis,
    save_recipe_from_agent,
)


def complete_coverage(**overrides):
    fields = {
        'calories': 1.0,
        'protein_g': 1.0,
        'carbohydrate_g': 1.0,
        'fat_g': 1.0,
        'fiber_g': 1.0,
        'sugar_g': 1.0,
        'sodium_mg': 1.0,
    }
    fields.update(overrides)
    return {'field_coverage': fields}


def test_per_serving_macro_calculation_is_deterministic():
    analysis = {
        'total': {
            'calories': 1200.0,
            'protein_g': 96.0,
            'carbohydrate_g': 140.0,
            'fat_g': 32.0,
            'fiber_g': 20.0,
            'sugar_g': 12.0,
            'sodium_mg': 1800.0,
        }
    }
    result = per_serving_from_analysis(analysis, 4)
    assert result['calories'] == 300.0
    assert result['protein_g'] == 24.0
    assert result['carbohydrate_g'] == 35.0
    assert result['fat_g'] == 8.0


def test_per_serving_requires_positive_servings():
    with pytest.raises(AgentRecipeInputError):
        per_serving_from_analysis({'total': {}}, 0)


def test_macro_constraints_can_express_low_cal_high_protein_target():
    per_serving = {
        'calories': 492.0,
        'protein_g': 52.0,
        'carbohydrate_g': 48.0,
        'fat_g': 12.0,
        'fiber_g': 7.0,
    }
    result = evaluate_macro_constraints(
        per_serving,
        complete_coverage(),
        {'calories_max': 500, 'protein_min_g': 50},
    )
    assert result['all_verifiable'] is True
    assert result['all_satisfied'] is True
    assert [check['satisfied'] for check in result['checks']] == [True, True]


def test_macro_constraints_report_failed_target():
    per_serving = {
        'calories': 540.0,
        'protein_g': 44.0,
        'carbohydrate_g': 48.0,
        'fat_g': 12.0,
        'fiber_g': 7.0,
    }
    result = evaluate_macro_constraints(
        per_serving,
        complete_coverage(),
        {'calories_max': 500, 'protein_min_g': 50},
    )
    assert result['all_verifiable'] is True
    assert result['all_satisfied'] is False
    assert all(check['satisfied'] is False for check in result['checks'])


def test_incomplete_nutrition_never_claims_target_is_satisfied():
    per_serving = {
        'calories': 420.0,
        'protein_g': 55.0,
        'carbohydrate_g': 40.0,
        'fat_g': 10.0,
        'fiber_g': 5.0,
    }
    result = evaluate_macro_constraints(
        per_serving,
        complete_coverage(calories=0.8),
        {'calories_max': 500, 'protein_min_g': 50},
    )
    calories = next(check for check in result['checks'] if check['constraint'] == 'calories_max')
    protein = next(check for check in result['checks'] if check['constraint'] == 'protein_min_g')
    assert calories['verifiable'] is False
    assert calories['satisfied'] is None
    assert protein['verifiable'] is True
    assert protein['satisfied'] is True
    assert result['all_verifiable'] is False
    assert result['all_satisfied'] is False


def test_non_macro_constraints_are_preserved_for_agent_reasoning():
    result = evaluate_macro_constraints(
        {'calories': 450.0, 'protein_g': 50.0},
        complete_coverage(),
        {
            'calories_max': 500,
            'inventory_policy': 'prefer_pantry',
            'preserve': ['chicken', 'main_flavour'],
        },
    )
    assert result['non_macro_constraints'] == {
        'inventory_policy': 'prefer_pantry',
        'preserve': ['chicken', 'main_flavour'],
    }


def test_constraint_targets_must_be_numeric():
    with pytest.raises(AgentRecipeInputError):
        evaluate_macro_constraints(
            {'calories': 450.0},
            complete_coverage(),
            {'calories_max': 'about five hundred'},
        )


def _agent_request(space, user, key, payload=None):
    return SimpleNamespace(
        space=space,
        user=user,
        user_space=None,
        headers={
            'X-Agent-Client': 'tandoor-mcp',
            'X-Request-ID': 'agent-test-request',
            'Idempotency-Key': key,
        },
        method='PATCH',
        path='/api/agent/recipes/1/',
        data=payload or {},
        query_params={},
    )


def test_failed_idempotency_key_can_be_retried_for_same_mutation(space_1, u1_s1):
    user = auth.get_user(u1_s1)
    request = _agent_request(
        space_1,
        user,
        'retry-recipe-update',
        {'expected_updated_at': 'revision-1', 'recipe': {'name': 'Updated'}},
    )

    with scopes_disabled():
        failed = AgentAuditEvent.objects.create(
            client_id='tandoor-mcp',
            action='recipe.update',
            request_id='failed-request',
            idempotency_key='retry-recipe-update',
            success=False,
            error='temporary failure',
            created_by=user,
            space=space_1,
        )

        success = record_agent_event(
            request,
            action='recipe.update',
            target_type='Recipe',
            target_id='1',
            response={'id': 1, 'name': 'Updated'},
        )

        failed.refresh_from_db()
        assert failed.idempotency_key == ''
        assert failed.metadata['released_idempotency_key'] == 'retry-recipe-update'
        assert success.idempotency_key == 'retry-recipe-update'
        assert success.success is True
        assert success.metadata['idempotency_fingerprint']


def test_failed_idempotency_key_rejects_different_mutation(space_1, u1_s1):
    user = auth.get_user(u1_s1)
    request = _agent_request(space_1, user, 'cross-action-key', {'recipe': {'name': 'Updated'}})

    with scopes_disabled():
        AgentAuditEvent.objects.create(
            client_id='tandoor-mcp',
            action='nutrition.profile.create',
            request_id='failed-request',
            idempotency_key='cross-action-key',
            success=False,
            created_by=user,
            space=space_1,
        )

        with pytest.raises(IntegrityError, match='failed action'):
            record_agent_event(
                request,
                action='recipe.update',
                target_type='Recipe',
                target_id='1',
            )


def test_partial_recipe_update_changes_existing_ingredients_without_dropping_siblings(
    space_1,
    u1_s1,
    recipe_1_s1,
):
    user = auth.get_user(u1_s1)

    with scopes_disabled():
        recipe = recipe_1_s1
        step = recipe.steps.order_by('order', 'id').first()
        ingredients = list(step.ingredients.order_by('order', 'id'))
        assert len(ingredients) >= 3

        original_step_ids = set(recipe.steps.values_list('id', flat=True))
        original_ingredient_ids = set(step.ingredients.values_list('id', flat=True))

        request = _agent_request(space_1, user, 'nested-update-regression')
        request.user_space = user.userspace_set.filter(space=space_1).first()

        save_recipe_from_agent(
            request,
            {
                'steps': [
                    {
                        'id': step.id,
                        'ingredients': [
                            {
                                'id': ingredients[0].id,
                                'food_id': ingredients[0].food_id,
                                'unit_id': ingredients[0].unit_id,
                                'amount': 1,
                            },
                            {
                                'id': ingredients[1].id,
                                'food_id': ingredients[1].food_id,
                                'unit_id': ingredients[1].unit_id,
                                'amount': 2,
                            },
                        ],
                    },
                ],
            },
            instance=recipe,
            partial=True,
        )

        ingredients[0].refresh_from_db()
        ingredients[1].refresh_from_db()
        recipe.refresh_from_db()

        assert ingredients[0].amount == Decimal('1')
        assert ingredients[1].amount == Decimal('2')
        assert set(recipe.steps.values_list('id', flat=True)) == original_step_ids
        assert set(step.ingredients.values_list('id', flat=True)) == original_ingredient_ids
