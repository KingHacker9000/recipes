from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib import auth
from django_scopes import scopes_disabled

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


def test_partial_recipe_update_changes_existing_ingredients_without_dropping_siblings(
    space_1,
    u1_s1,
    recipe_1_s1,
):
    """Regression for the MCP recipe_update that was misreported as HTTP 409.

    Decimal normalization previously reached CustomDecimalField as a Decimal,
    which it converted to None. The nested serializer then attempted to write a
    NULL ingredient amount, and the view's broad IntegrityError handler labeled
    that database error as an idempotency conflict. A partial nested list could
    also remove omitted sibling ingredients.
    """
    user = auth.get_user(u1_s1)

    with scopes_disabled():
        recipe = recipe_1_s1
        step = recipe.steps.order_by('order', 'id').first()
        ingredients = list(step.ingredients.order_by('order', 'id'))
        assert len(ingredients) >= 3

        first, second = ingredients[:2]
        first.amount = Decimal('0')
        second.amount = Decimal('0')
        first.save(update_fields=['amount'])
        second.save(update_fields=['amount'])

        untouched = ingredients[2]
        untouched_amount = untouched.amount
        untouched_note = untouched.note

        original_step_ids = set(recipe.steps.values_list('id', flat=True))
        original_ingredient_ids = set(step.ingredients.values_list('id', flat=True))

        request = SimpleNamespace(
            space=space_1,
            user=user,
            user_space=user.userspace_set.filter(space=space_1).first(),
            headers={
                'X-Agent-Client': 'tandoor-mcp',
                'X-Request-ID': 'recipe-update-regression',
                'Idempotency-Key': 'fix-mugcake-nutrition-qty-regression',
            },
            method='PATCH',
            path=f'/api/agent/recipes/{recipe.id}/',
            data={},
            query_params={},
        )

        save_recipe_from_agent(
            request,
            {
                'steps': [
                    {
                        'id': step.id,
                        'ingredients': [
                            {
                                'id': first.id,
                                'food_id': first.food_id,
                                'unit_id': first.unit_id,
                                'amount': 1,
                            },
                            {
                                'id': second.id,
                                'food_id': second.food_id,
                                'unit_id': second.unit_id,
                                'amount': 2,
                            },
                        ],
                    },
                ],
            },
            instance=recipe,
            partial=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        untouched.refresh_from_db()
        recipe.refresh_from_db()

        assert first.amount == Decimal('1')
        assert second.amount == Decimal('2')
        assert untouched.amount == untouched_amount
        assert untouched.note == untouched_note
        assert set(recipe.steps.values_list('id', flat=True)) == original_step_ids
        assert set(step.ingredients.values_list('id', flat=True)) == original_ingredient_ids
