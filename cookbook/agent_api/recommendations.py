from decimal import Decimal, InvalidOperation

from django.db.models import Q

from cookbook.agent_api.nutrition import analyze_recipe
from cookbook.agent_api.pantry import check_recipe_against_pantry
from cookbook.models import Recipe


class AgentRecommendationInputError(ValueError):
    pass


def _decimal(value, field, *, minimum=None):
    if value in (None, ''):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AgentRecommendationInputError(f'{field} must be numeric.')
    if minimum is not None and result < Decimal(str(minimum)):
        raise AgentRecommendationInputError(f'{field} must be at least {minimum}.')
    return result


def _accessible_recipes(request):
    return (Recipe.objects
            .filter(space=request.space)
            .filter(Q(private=False) | Q(created_by=request.user) | Q(shared=request.user))
            .prefetch_related('shared', 'steps__ingredients__food', 'steps__ingredients__unit')
            .distinct())


def _macro_check(analysis, *, calories_max=None, protein_min_g=None):
    per_serving = analysis.get('per_serving') or {}
    coverage = (analysis.get('coverage') or {}).get('field_coverage') or {}
    checks = []

    if calories_max is not None:
        actual = per_serving.get('calories')
        verifiable = actual is not None and Decimal(str(coverage.get('calories', 0))) >= 1
        checks.append({
            'field': 'calories',
            'operator': 'max',
            'target': float(calories_max),
            'actual': actual,
            'verifiable': verifiable,
            'satisfied': bool(verifiable and Decimal(str(actual)) <= calories_max),
        })
    if protein_min_g is not None:
        actual = per_serving.get('protein_g')
        verifiable = actual is not None and Decimal(str(coverage.get('protein_g', 0))) >= 1
        checks.append({
            'field': 'protein_g',
            'operator': 'min',
            'target': float(protein_min_g),
            'actual': actual,
            'verifiable': verifiable,
            'satisfied': bool(verifiable and Decimal(str(actual)) >= protein_min_g),
        })

    return {
        'checks': checks,
        'all_verifiable': all(check['verifiable'] for check in checks) if checks else True,
        'all_satisfied': all(check['satisfied'] for check in checks) if checks else True,
    }


def recommend_recipes(request, *, query='', target_servings=None, calories_max=None, protein_min_g=None, limit=10):
    target_servings = _decimal(target_servings, 'target_servings', minimum='0.0001')
    calories_max = _decimal(calories_max, 'calories_max', minimum=0)
    protein_min_g = _decimal(protein_min_g, 'protein_min_g', minimum=0)
    try:
        limit = min(max(int(limit), 1), 25)
    except (TypeError, ValueError):
        raise AgentRecommendationInputError('limit must be an integer between 1 and 25.')

    queryset = _accessible_recipes(request).order_by('name')
    query = str(query or '').strip()
    if query:
        queryset = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))

    # Bound expensive deterministic analysis. We rank the first 100 textual
    # candidates and return at most 25.
    results = []
    for recipe in queryset[:100]:
        pantry = check_recipe_against_pantry(recipe, request.space, target_servings=target_servings)
        nutrition = analyze_recipe(recipe, request.space)
        macros = _macro_check(nutrition, calories_max=calories_max, protein_min_g=protein_min_g)
        pantry_fraction = Decimal(str((pantry.get('coverage') or {}).get('complete_fraction', 0)))
        known_fraction = Decimal(str((pantry.get('coverage') or {}).get('known_fraction', 0)))
        macro_bonus = Decimal('1') if macros['all_satisfied'] and macros['all_verifiable'] else Decimal('0')
        macro_known = Decimal('1') if macros['all_verifiable'] else Decimal('0')
        score = pantry_fraction * Decimal('70') + known_fraction * Decimal('10') + macro_bonus * Decimal('15') + macro_known * Decimal('5')
        results.append({
            'recipe_id': recipe.id,
            'name': recipe.name,
            'servings': recipe.servings,
            'score': round(float(score), 2),
            'pantry': pantry,
            'nutrition': {
                'per_serving': nutrition.get('per_serving'),
                'coverage': nutrition.get('coverage'),
            },
            'macro_fit': macros,
        })

    results.sort(
        key=lambda item: (
            item['macro_fit']['all_satisfied'] and item['macro_fit']['all_verifiable'],
            item['pantry']['can_make'],
            item['score'],
            item['name'].lower(),
        ),
        reverse=True,
    )
    return {
        'query': query,
        'target_servings': float(target_servings) if target_servings is not None else None,
        'constraints': {
            'calories_max': float(calories_max) if calories_max is not None else None,
            'protein_min_g': float(protein_min_g) if protein_min_g is not None else None,
        },
        'results': results[:limit],
        'ranking_note': 'Ranking is deterministic: pantry completeness, known pantry coverage, and fully verifiable macro fit.',
    }
