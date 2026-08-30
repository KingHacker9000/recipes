from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.agent_api.pantry import AgentPantryInputError, check_recipe_against_pantry, inventory_queryset
from cookbook.agent_api.recommendations import AgentRecommendationInputError, recommend_recipes
from cookbook.agent_api.recipes import AgentRecipeInputError
from cookbook.agent_api.scaling import practical_scale_preview
from cookbook.helper.permission_helper import CustomIsUser, CustomRecipePermission, CustomTokenHasReadWriteScope
from cookbook.models import Recipe


AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]


class AgentCompleteHealthView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        return Response({
            'version': '2026-08-30.v2',
            'space_id': request.space.id,
            'capabilities': {
                'recipes': [
                    'search', 'get', 'create', 'update', 'clone', 'nutrition_analyze',
                    'exact_scale_preview', 'practical_scale_preview', 'recommend',
                    'pantry_check', 'substitution_context', 'variant_preview', 'variant_save',
                ],
                'foods': ['search', 'nutrition_profile_read', 'nutrition_profile_write', 'fdc_search', 'fdc_verify'],
                'nutrition': ['evaluate_draft', 'coverage', 'provenance'],
                'pantry': ['locations', 'entries', 'adjust_delta', 'reconcile_preview', 'proposal_apply'],
                'shopping': ['lists', 'entry_create', 'entry_update', 'entry_delete'],
                'meal_plans': ['meal_types', 'list', 'create', 'delete'],
                'audit': ['list', 'idempotency'],
                'mcp': ['semantic_tools', 'stdio', 'authenticated_streamable_http'],
            },
        })


def _recipe(view, request, pk):
    obj = (Recipe.objects
           .filter(pk=pk, space=request.space)
           .filter(Q(private=False) | Q(created_by=request.user) | Q(shared=request.user))
           .prefetch_related('shared', 'steps__ingredients__food__substitute', 'steps__ingredients__unit')
           .distinct()
           .first())
    if obj is None or not CustomRecipePermission().has_object_permission(request, view, obj):
        return None
    return obj


def _error(exc):
    return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class AgentPracticalScaleView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, pk):
        recipe = _recipe(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            result = practical_scale_preview(
                recipe,
                request.data.get('target_servings'),
                request.space,
                count_step=request.data.get('count_step', 1),
                count_rounding=str(request.data.get('count_rounding') or 'nearest'),
            )
        except AgentRecipeInputError as exc:
            return _error(exc)
        return Response(result)


class AgentRecipeRecommendationView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request):
        try:
            result = recommend_recipes(
                request,
                query=request.data.get('query') or '',
                target_servings=request.data.get('target_servings'),
                calories_max=request.data.get('calories_max'),
                protein_min_g=request.data.get('protein_min_g'),
                limit=request.data.get('limit', 10),
            )
        except (AgentRecommendationInputError, AgentPantryInputError) as exc:
            return _error(exc)
        return Response(result)


class AgentRecipeSubstitutionContextView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, pk):
        recipe = _recipe(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            pantry = check_recipe_against_pantry(
                recipe,
                request.space,
                target_servings=request.data.get('target_servings'),
            )
        except AgentPantryInputError as exc:
            return _error(exc)

        by_ingredient = {}
        for step in recipe.steps.all():
            for ingredient in step.ingredients.all():
                by_ingredient[ingredient.id] = ingredient

        result = []
        for item in pantry['ingredients']:
            if item['status'] not in ('missing', 'partial', 'unknown') or not item.get('food_id'):
                continue
            ingredient = by_ingredient.get(item['ingredient_id'])
            configured = []
            if ingredient and ingredient.food_id:
                for substitute in ingredient.food.substitute.filter(space=request.space).order_by('name')[:25]:
                    entries = list(inventory_queryset(request.space).filter(food=substitute, amount__gt=0).order_by('expires', 'id'))
                    configured.append({
                        'food_id': substitute.id,
                        'food': substitute.name,
                        'in_pantry': bool(entries),
                        'inventory_entries': [
                            {
                                'entry_id': entry.id,
                                'amount': float(entry.amount),
                                'unit': entry.unit.name if entry.unit_id else '',
                                'location': entry.inventory_location.name,
                                'expires': entry.expires.isoformat() if entry.expires else None,
                            }
                            for entry in entries
                        ],
                    })
            result.append({
                'ingredient': item,
                'configured_substitutes': configured,
                'note': 'Only Tandoor-configured substitutes are returned; culinary alternatives may be proposed by the agent and re-evaluated before save.',
            })
        return Response({'recipe_id': recipe.id, 'items': result})
