from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from cookbook.agent_api.nutrition import evaluate_draft, unit_descriptor
from cookbook.agent_api.recipes import AgentRecipeInputError, recipe_to_agent_input


def _decimal(value, field):
    try:
        result = Decimal(str(value))
    except Exception:
        raise AgentRecipeInputError(f'{field} must be numeric.')
    if result <= 0:
        raise AgentRecipeInputError(f'{field} must be greater than zero.')
    return result


def _round_to_step(value, step, mode):
    quotient = value / step
    if mode == 'up':
        rounded = quotient.to_integral_value(rounding=ROUND_CEILING)
    elif mode == 'down':
        rounded = quotient.to_integral_value(rounding=ROUND_FLOOR)
    else:
        rounded = quotient.to_integral_value(rounding=ROUND_HALF_UP)
    return rounded * step


def practical_scale_preview(recipe, target_servings, space, *, count_step=1, count_rounding='nearest'):
    """Return an agent-editable scaled recipe without silently guessing custom units.

    Mass and volume quantities are scaled exactly. Count units are rounded to an
    explicit configurable step. Custom units remain exact and are surfaced as
    warnings so the calling agent can make a culinary decision.
    """
    target = _decimal(target_servings, 'target_servings')
    current = Decimal(str(recipe.servings if recipe.servings and recipe.servings > 0 else 1))
    factor = target / current
    step = _decimal(count_step, 'count_step')
    if count_rounding not in ('nearest', 'up', 'down'):
        raise AgentRecipeInputError('count_rounding must be nearest, up, or down.')

    candidate = deepcopy(recipe_to_agent_input(recipe, name=recipe.name))
    candidate['servings'] = target
    adjustments = []
    warnings = []
    draft_items = []

    for step_data in candidate.get('steps') or []:
        for ingredient in step_data.get('ingredients') or []:
            original = Decimal(str(ingredient.get('amount') or 0))
            exact = original * factor
            unit_name = ''
            unit_id = ingredient.get('unit_id')
            if unit_id:
                unit = recipe.space.unit_set.filter(pk=unit_id).first() if hasattr(recipe.space, 'unit_set') else None
                if unit:
                    unit_name = unit.name
            descriptor = unit_descriptor(unit_name)
            practical = exact
            reason = 'exact_scale'
            if descriptor['dimension'] == 'count' and exact > 0:
                practical = _round_to_step(exact, step, count_rounding)
                reason = 'discrete_count_rounding'
            elif descriptor['dimension'] == 'custom' and exact > 0:
                warnings.append({
                    'food_id': ingredient.get('food_id'),
                    'unit': unit_name,
                    'reason': 'custom_unit_requires_culinary_review',
                    'exact_amount': float(exact),
                })
            ingredient['amount'] = practical
            adjustments.append({
                'food_id': ingredient.get('food_id'),
                'unit_id': unit_id,
                'unit': unit_name,
                'original_amount': float(original),
                'exact_scaled_amount': float(exact),
                'practical_amount': float(practical),
                'reason': reason,
            })
            draft_items.append({
                'food_id': ingredient.get('food_id'),
                'amount': practical,
                'unit': unit_name,
            })

    nutrition = evaluate_draft(draft_items, space)
    per_serving = {}
    for field, value in nutrition.get('total', {}).items():
        per_serving[field] = None if value is None else round(float(Decimal(str(value)) / target), 2)
    nutrition['per_serving'] = per_serving

    return {
        'recipe_id': recipe.id,
        'recipe_name': recipe.name,
        'mode': 'practical_preview',
        'current_servings': float(current),
        'target_servings': float(target),
        'scale_factor': float(factor),
        'count_step': float(step),
        'count_rounding': count_rounding,
        'candidate': candidate,
        'adjustments': adjustments,
        'warnings': warnings,
        'nutrition': nutrition,
        'note': 'Preview only. Custom-unit and culinary changes must be reviewed before saving a recipe or variant.',
    }
