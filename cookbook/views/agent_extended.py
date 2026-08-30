from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.agent_api.audit import find_idempotent_replay, record_agent_event
from cookbook.agent_api.household import (
    AgentHouseholdInputError,
    create_meal_plan,
    create_shopping_entry,
    create_shopping_list,
    meal_plan_payload,
    meal_type_payload,
    shopping_entry_payload,
    shopping_list_payload,
    update_shopping_entry,
)
from cookbook.agent_api.models import FoodNutritionProfile
from cookbook.agent_api.nutrition_sources import (
    FoodDataCentralError,
    FoodDataCentralNotFound,
    food_details,
    invalidate_food_cache,
    nutrition_profile_from_fdc,
    search_foods,
)
from cookbook.agent_api.pantry import AgentPantryInputError, check_recipe_against_pantry, inventory_queryset
from cookbook.agent_api.pantry_adjust import adjust_inventory
from cookbook.agent_api.recommendations import AgentRecommendationInputError, recommend_recipes
from cookbook.agent_api.scaling import practical_scale_preview
from cookbook.agent_api.recipes import AgentRecipeInputError
from cookbook.helper.permission_helper import CustomIsUser, CustomRecipePermission, CustomTokenHasReadWriteScope
from cookbook.models import Food, MealPlan, MealType, Recipe, ShoppingList, ShoppingListEntry


AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]


def _replay(request):
    event = find_idempotent_replay(request)
    if event is None:
        return None
    payload = dict(event.response or {})
    payload['idempotent_replay'] = True
    return Response(payload, status=status.HTTP_200_OK)


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


def _error(exc, *, conflict=False):
    return Response(
        {'error': True, 'msg': str(exc)},
        status=status.HTTP_409_CONFLICT if conflict else status.HTTP_400_BAD_REQUEST,
    )


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


class AgentPantryAdjustView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request):
        replay = _replay(request)
        if replay:
            return replay
        try:
            before = None
            if request.data.get('entry_id'):
                entry = inventory_queryset(request.space).filter(pk=request.data.get('entry_id')).first()
                if entry:
                    before = {'entry_id': entry.id, 'amount': float(entry.amount), 'updated_at': entry.updated_at.isoformat()}
            response = adjust_inventory(request, request.data)
            record_agent_event(
                request,
                action='pantry.adjust',
                target_type='InventoryEntry',
                target_id=response['id'],
                before=before or {},
                after=response,
                response=response,
                metadata={'delta': request.data.get('delta'), 'reason': request.data.get('reason') or ''},
            )
        except AgentPantryInputError as exc:
            conflict = 'changed since' in str(exc).lower()
            return _error(exc, conflict=conflict)
        except IntegrityError:
            return Response({'error': True, 'msg': 'The idempotency key conflicts with an existing write.'}, status=status.HTTP_409_CONFLICT)
        return Response(response)


class AgentShoppingListCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        values = ShoppingList.objects.filter(space=request.space).order_by('name', 'id')
        return Response([shopping_list_payload(obj) for obj in values])

    def post(self, request):
        replay = _replay(request)
        if replay:
            return replay
        try:
            with transaction.atomic():
                obj = create_shopping_list(request, request.data)
                response = shopping_list_payload(obj)
                record_agent_event(request, action='shopping_list.create', target_type='ShoppingList', target_id=obj.id, after=response, response=response)
        except (AgentHouseholdInputError, IntegrityError) as exc:
            return _error(exc)
        return Response(response, status=status.HTTP_201_CREATED)


class AgentShoppingEntryCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        values = (ShoppingListEntry.objects
                  .filter(space=request.space)
                  .select_related('food', 'unit')
                  .prefetch_related('shopping_lists')
                  .order_by('checked', 'created_at', 'id'))
        if request.query_params.get('shopping_list_id'):
            values = values.filter(shopping_lists__id=request.query_params.get('shopping_list_id'))
        if str(request.query_params.get('include_checked') or '').lower() not in ('1', 'true', 'yes'):
            values = values.filter(checked=False)
        return Response([shopping_entry_payload(obj) for obj in values[:500]])

    def post(self, request):
        replay = _replay(request)
        if replay:
            return replay
        try:
            obj = create_shopping_entry(request, request.data)
            obj = ShoppingListEntry.objects.select_related('food', 'unit').prefetch_related('shopping_lists').get(pk=obj.pk)
            response = shopping_entry_payload(obj)
            record_agent_event(request, action='shopping_entry.create', target_type='ShoppingListEntry', target_id=obj.id, after=response, response=response)
        except (AgentHouseholdInputError, IntegrityError) as exc:
            return _error(exc)
        return Response(response, status=status.HTTP_201_CREATED)


class AgentShoppingEntryDetailView(APIView):
    permission_classes = AGENT_PERMISSION

    def _obj(self, request, pk):
        return (ShoppingListEntry.objects
                .filter(pk=pk, space=request.space)
                .select_related('food', 'unit')
                .prefetch_related('shopping_lists')
                .first())

    def patch(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay
        obj = self._obj(request, pk)
        if obj is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        before = shopping_entry_payload(obj)
        try:
            with transaction.atomic():
                locked = ShoppingListEntry.objects.select_for_update().get(pk=obj.pk, space=request.space)
                locked = update_shopping_entry(request, locked, request.data)
                locked = self._obj(request, locked.pk)
                response = shopping_entry_payload(locked)
                record_agent_event(request, action='shopping_entry.update', target_type='ShoppingListEntry', target_id=locked.id, before=before, after=response, response=response)
        except AgentHouseholdInputError as exc:
            return _error(exc, conflict='changed since' in str(exc).lower())
        return Response(response)

    def delete(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay
        if request.data.get('confirmed') is not True:
            return _error(AgentHouseholdInputError('confirmed=true is required to delete a shopping entry.'))
        obj = self._obj(request, pk)
        if obj is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        before = shopping_entry_payload(obj)
        obj.delete()
        response = {'deleted': True, 'id': pk}
        record_agent_event(request, action='shopping_entry.delete', target_type='ShoppingListEntry', target_id=pk, before=before, response=response)
        return Response(response)


class AgentMealTypeCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        values = MealType.objects.filter(space=request.space).order_by('order', 'name')
        return Response([meal_type_payload(obj) for obj in values])


class AgentMealPlanCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        values = (MealPlan.objects
                  .filter(space=request.space)
                  .select_related('recipe', 'meal_type')
                  .order_by('from_date', 'id'))
        if request.query_params.get('from'):
            values = values.filter(to_date__gte=request.query_params.get('from'))
        if request.query_params.get('to'):
            values = values.filter(from_date__lte=request.query_params.get('to'))
        return Response([meal_plan_payload(obj) for obj in values[:500]])

    def post(self, request):
        replay = _replay(request)
        if replay:
            return replay
        try:
            obj = create_meal_plan(request, request.data)
            obj = MealPlan.objects.select_related('recipe', 'meal_type').get(pk=obj.pk)
            response = meal_plan_payload(obj)
            record_agent_event(request, action='meal_plan.create', target_type='MealPlan', target_id=obj.id, after=response, response=response)
        except (AgentHouseholdInputError, IntegrityError) as exc:
            return _error(exc)
        return Response(response, status=status.HTTP_201_CREATED)


class AgentMealPlanDetailView(APIView):
    permission_classes = AGENT_PERMISSION

    def delete(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay
        if request.data.get('confirmed') is not True:
            return _error(AgentHouseholdInputError('confirmed=true is required to delete a meal plan.'))
        obj = MealPlan.objects.filter(pk=pk, space=request.space).select_related('recipe', 'meal_type').first()
        if obj is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        before = meal_plan_payload(obj)
        obj.delete()
        response = {'deleted': True, 'id': pk}
        record_agent_event(request, action='meal_plan.delete', target_type='MealPlan', target_id=pk, before=before, response=response)
        return Response(response)


class AgentFdcSearchView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        query = request.query_params.get('q') or request.query_params.get('query')
        try:
            return Response(search_foods(query, page_size=request.query_params.get('limit', 10)))
        except FoodDataCentralError as exc:
            return _error(exc)


class AgentFoodFdcVerifyView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay
        food = Food.objects.filter(pk=pk, space=request.space).first()
        if food is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if request.data.get('confirmed') is not True:
            return _error(FoodDataCentralError('confirmed=true is required before persisting an FDC match.'))
        fdc_id = request.data.get('fdc_id')
        try:
            payload = food_details(fdc_id, force_refresh=bool(request.data.get('force_refresh', False)))
            normalized = nutrition_profile_from_fdc(payload)
        except FoodDataCentralNotFound as exc:
            try:
                invalidate_food_cache(fdc_id)
            except Exception:
                pass
            if food.fdc_id and str(food.fdc_id) == str(fdc_id):
                food.fdc_id = None
                food.save(update_fields=['fdc_id'])
            return Response({'error': True, 'msg': str(exc), 'stale_fdc_id_cleared': True}, status=status.HTTP_404_NOT_FOUND)
        except FoodDataCentralError as exc:
            return _error(exc)

        source_type = FoodNutritionProfile.SOURCE_BRANDED if str(normalized.get('data_type')).lower() == 'branded' else FoodNutritionProfile.SOURCE_REFERENCE
        is_default = request.data.get('is_default', True) is not False
        try:
            with transaction.atomic():
                if is_default:
                    FoodNutritionProfile.objects.filter(space=request.space, food=food, is_default=True).update(is_default=False)
                profile = FoodNutritionProfile.objects.create(
                    food=food,
                    label=normalized.get('description', '')[:256],
                    brand=(normalized.get('brand_name') or normalized.get('brand_owner') or '')[:256],
                    barcode=str(normalized.get('gtin_upc') or '')[:128],
                    basis_amount=normalized['basis_amount'],
                    basis_unit=normalized['basis_unit'],
                    calories=normalized.get('calories'),
                    protein_g=normalized.get('protein_g'),
                    carbohydrate_g=normalized.get('carbohydrate_g'),
                    fat_g=normalized.get('fat_g'),
                    fiber_g=normalized.get('fiber_g'),
                    sugar_g=normalized.get('sugar_g'),
                    sodium_mg=normalized.get('sodium_mg'),
                    source_type=source_type,
                    source_reference=f'USDA FDC:{normalized.get("fdc_id")}',
                    confidence=1,
                    verified=True,
                    is_default=is_default,
                    created_by=request.user,
                    space=request.space,
                )
                food.fdc_id = int(normalized['fdc_id'])
                food.save(update_fields=['fdc_id'])
                response = {
                    'food_id': food.id,
                    'fdc_id': food.fdc_id,
                    'profile_id': profile.id,
                    'source_type': profile.source_type,
                    'verified': profile.verified,
                    'is_default': profile.is_default,
                    'basis_amount': float(profile.basis_amount),
                    'basis_unit': profile.basis_unit,
                    'calories': float(profile.calories) if profile.calories is not None else None,
                    'protein_g': float(profile.protein_g) if profile.protein_g is not None else None,
                    'carbohydrate_g': float(profile.carbohydrate_g) if profile.carbohydrate_g is not None else None,
                    'fat_g': float(profile.fat_g) if profile.fat_g is not None else None,
                    'fiber_g': float(profile.fiber_g) if profile.fiber_g is not None else None,
                    'sugar_g': float(profile.sugar_g) if profile.sugar_g is not None else None,
                    'sodium_mg': float(profile.sodium_mg) if profile.sodium_mg is not None else None,
                }
                record_agent_event(
                    request,
                    action='nutrition_profile.fdc_verify',
                    target_type='FoodNutritionProfile',
                    target_id=profile.id,
                    after=response,
                    response=response,
                    metadata={'food_id': food.id, 'fdc_id': food.fdc_id},
                )
        except IntegrityError:
            return Response({'error': True, 'msg': 'The idempotency key or nutrition profile conflicts with an existing write.'}, status=status.HTTP_409_CONFLICT)
        return Response(response, status=status.HTTP_201_CREATED)
