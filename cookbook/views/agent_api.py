from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.agent_api.audit import find_idempotent_replay, record_agent_event
from cookbook.agent_api.models import AgentAuditEvent, FoodNutritionProfile
from cookbook.agent_api.nutrition import analyze_recipe, evaluate_draft, profile_dict, scale_recipe_preview
from cookbook.helper.permission_helper import CustomIsUser, CustomRecipePermission, CustomTokenHasReadWriteScope
from cookbook.models import Food, Recipe


AGENT_API_VERSION = '2026-08-30.v1'
AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]


def _decimal(value, *, field, allow_null=True, minimum=None, maximum=None):
    if value in (None, ''):
        if allow_null:
            return None
        raise ValueError(f'{field} is required.')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f'{field} must be numeric.')
    if minimum is not None and result < Decimal(str(minimum)):
        raise ValueError(f'{field} must be at least {minimum}.')
    if maximum is not None and result > Decimal(str(maximum)):
        raise ValueError(f'{field} must be at most {maximum}.')
    return result


def _profile_payload(profile):
    value = profile_dict(profile)
    value.update({
        'grams_per_ml': float(profile.grams_per_ml) if profile.grams_per_ml is not None else None,
        'calories': float(profile.calories) if profile.calories is not None else None,
        'protein_g': float(profile.protein_g) if profile.protein_g is not None else None,
        'carbohydrate_g': float(profile.carbohydrate_g) if profile.carbohydrate_g is not None else None,
        'fat_g': float(profile.fat_g) if profile.fat_g is not None else None,
        'fiber_g': float(profile.fiber_g) if profile.fiber_g is not None else None,
        'sugar_g': float(profile.sugar_g) if profile.sugar_g is not None else None,
        'sodium_mg': float(profile.sodium_mg) if profile.sodium_mg is not None else None,
        'created_at': profile.created_at.isoformat(),
        'updated_at': profile.updated_at.isoformat(),
    })
    return value


def _recipe_queryset(request):
    return (Recipe.objects
            .filter(space=request.space)
            .filter(Q(private=False) | Q(created_by=request.user) | Q(shared=request.user))
            .distinct())


def _recipe_for_request(view, request, pk):
    recipe = (_recipe_queryset(request)
              .filter(pk=pk)
              .prefetch_related('shared', 'steps__ingredients__food', 'steps__ingredients__unit')
              .first())
    if recipe is None:
        return None
    # Reuse Tandoor's recipe object permission as a second guard. The Agent API
    # intentionally does not invent a weaker authorization model.
    if not CustomRecipePermission().has_object_permission(request, view, recipe):
        return None
    return recipe


def _recipe_payload(recipe, include_steps=False):
    value = {
        'id': recipe.id,
        'name': recipe.name,
        'description': recipe.description or '',
        'servings': recipe.servings,
        'servings_text': recipe.servings_text or '',
        'private': recipe.private,
        'created_by_id': recipe.created_by_id,
        'updated_at': recipe.updated_at.isoformat() if getattr(recipe, 'updated_at', None) else None,
    }
    if include_steps:
        steps = []
        for step in recipe.steps.all().order_by('order', 'id'):
            ingredients = []
            for ingredient in step.ingredients.all().order_by('id'):
                ingredients.append({
                    'id': ingredient.id,
                    'food_id': ingredient.food_id,
                    'food': ingredient.food.name if ingredient.food_id else '',
                    'amount': float(ingredient.amount),
                    'unit_id': ingredient.unit_id,
                    'unit': ingredient.unit.name if ingredient.unit_id else '',
                    'note': ingredient.note or '',
                })
            steps.append({
                'id': step.id,
                'name': step.name,
                'order': step.order,
                'instruction': step.instruction,
                'ingredients': ingredients,
            })
        value['steps'] = steps
    return value


class AgentHealthView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        return Response({
            'version': AGENT_API_VERSION,
            'space_id': request.space.id,
            'capabilities': {
                'recipes': ['search', 'get', 'nutrition_analyze', 'scale_preview'],
                'foods': ['search', 'nutrition_profile_read', 'nutrition_profile_write'],
                'nutrition': ['evaluate_draft'],
                'audit': ['list'],
                'proposals': ['foundation'],
                'variants': ['lineage_foundation'],
            },
        })


class AgentRecipeCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        query = str(request.query_params.get('q') or request.query_params.get('query') or '').strip()
        try:
            limit = min(max(int(request.query_params.get('limit', 20)), 1), 50)
        except (TypeError, ValueError):
            limit = 20
        queryset = _recipe_queryset(request).order_by('name')
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(description__icontains=query))
        return Response([_recipe_payload(recipe) for recipe in queryset[:limit]])


class AgentRecipeDetailView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request, pk):
        recipe = _recipe_for_request(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(_recipe_payload(recipe, include_steps=True))


class AgentRecipeNutritionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request, pk):
        recipe = _recipe_for_request(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(analyze_recipe(recipe, request.space))


class AgentRecipeScalePreviewView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, pk):
        recipe = _recipe_for_request(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            result = scale_recipe_preview(recipe, request.data.get('target_servings'), request.space)
        except ValueError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class AgentFoodSearchView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        query = str(request.query_params.get('q') or request.query_params.get('query') or '').strip()
        try:
            limit = min(max(int(request.query_params.get('limit', 20)), 1), 50)
        except (TypeError, ValueError):
            limit = 20
        queryset = Food.objects.filter(space=request.space).order_by('name')
        if query:
            queryset = queryset.filter(name__icontains=query)
        foods = []
        for food in queryset[:limit]:
            profile = (FoodNutritionProfile.objects
                       .filter(food=food, space=request.space)
                       .order_by('-is_default', '-verified', '-confidence', '-updated_at')
                       .first())
            foods.append({
                'id': food.id,
                'name': food.name,
                'nutrition_profile': profile_dict(profile),
            })
        return Response(foods)


class AgentFoodNutritionProfileCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        queryset = FoodNutritionProfile.objects.filter(space=request.space).select_related('food')
        food_id = request.query_params.get('food_id')
        if food_id:
            queryset = queryset.filter(food_id=food_id)
        return Response([_profile_payload(profile) for profile in queryset[:200]])

    def post(self, request):
        replay = find_idempotent_replay(request)
        if replay is not None:
            response = dict(replay.response or {})
            response['idempotent_replay'] = True
            return Response(response, status=status.HTTP_200_OK)

        food = Food.objects.filter(pk=request.data.get('food_id'), space=request.space).first()
        if food is None:
            return Response({'error': True, 'msg': 'Food not found.'}, status=status.HTTP_404_NOT_FOUND)

        try:
            basis_amount = _decimal(request.data.get('basis_amount', 100), field='basis_amount', allow_null=False, minimum='0.000001')
            confidence = _decimal(request.data.get('confidence', 1), field='confidence', allow_null=False, minimum=0, maximum=1)
            grams_per_ml = _decimal(request.data.get('grams_per_ml'), field='grams_per_ml', minimum='0.00000001')
            nutrient_values = {
                field: _decimal(request.data.get(field), field=field, minimum=0)
                for field in ('calories', 'protein_g', 'carbohydrate_g', 'fat_g', 'fiber_g', 'sugar_g', 'sodium_mg')
            }
        except ValueError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        basis_unit = str(request.data.get('basis_unit') or 'g').strip()[:64]
        if not basis_unit:
            return Response({'error': True, 'msg': 'basis_unit is required.'}, status=status.HTTP_400_BAD_REQUEST)
        source_type = str(request.data.get('source_type') or FoodNutritionProfile.SOURCE_MANUAL)
        if source_type not in dict(FoodNutritionProfile.SOURCE_CHOICES):
            return Response({'error': True, 'msg': 'Unsupported source_type.'}, status=status.HTTP_400_BAD_REQUEST)

        is_default = bool(request.data.get('is_default', False))
        try:
            with transaction.atomic():
                if is_default:
                    FoodNutritionProfile.objects.filter(food=food, space=request.space, is_default=True).update(is_default=False)
                profile = FoodNutritionProfile.objects.create(
                    food=food,
                    label=str(request.data.get('label') or '')[:256],
                    brand=str(request.data.get('brand') or '')[:256],
                    barcode=str(request.data.get('barcode') or '')[:128],
                    basis_amount=basis_amount,
                    basis_unit=basis_unit,
                    grams_per_ml=grams_per_ml,
                    source_type=source_type,
                    source_reference=str(request.data.get('source_reference') or '')[:2048],
                    confidence=confidence,
                    verified=bool(request.data.get('verified', False)),
                    is_default=is_default,
                    created_by=request.user,
                    space=request.space,
                    **nutrient_values,
                )
                response = _profile_payload(profile)
                record_agent_event(
                    request,
                    action='nutrition_profile.create',
                    target_type='FoodNutritionProfile',
                    target_id=profile.id,
                    after=response,
                    response=response,
                    metadata={'food_id': food.id},
                )
        except IntegrityError:
            return Response({'error': True, 'msg': 'The idempotency key or default profile conflicts with an existing write.'}, status=status.HTTP_409_CONFLICT)
        return Response(response, status=status.HTTP_201_CREATED)


class AgentFoodNutritionProfileDetailView(APIView):
    permission_classes = AGENT_PERMISSION

    def get_object(self, request, pk):
        return FoodNutritionProfile.objects.filter(pk=pk, space=request.space).select_related('food').first()

    def get(self, request, pk):
        profile = self.get_object(request, pk)
        if profile is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(_profile_payload(profile))

    def patch(self, request, pk):
        replay = find_idempotent_replay(request)
        if replay is not None:
            response = dict(replay.response or {})
            response['idempotent_replay'] = True
            return Response(response, status=status.HTTP_200_OK)

        try:
            with transaction.atomic():
                profile = (FoodNutritionProfile.objects
                           .select_for_update()
                           .filter(pk=pk, space=request.space)
                           .select_related('food')
                           .first())
                if profile is None:
                    return Response(status=status.HTTP_404_NOT_FOUND)

                expected_updated_at = str(request.data.get('expected_updated_at') or '').strip()
                if expected_updated_at and profile.updated_at.isoformat() != expected_updated_at:
                    return Response(
                        {'error': True, 'msg': 'Nutrition profile changed since it was read.', 'current': _profile_payload(profile)},
                        status=status.HTTP_409_CONFLICT,
                    )

                before = _profile_payload(profile)
                for field, max_length in (('label', 256), ('brand', 256), ('barcode', 128), ('basis_unit', 64), ('source_reference', 2048)):
                    if field in request.data:
                        value = str(request.data.get(field) or '').strip()[:max_length]
                        if field == 'basis_unit' and not value:
                            raise ValueError('basis_unit cannot be blank.')
                        setattr(profile, field, value)

                if 'basis_amount' in request.data:
                    profile.basis_amount = _decimal(request.data.get('basis_amount'), field='basis_amount', allow_null=False, minimum='0.000001')
                if 'confidence' in request.data:
                    profile.confidence = _decimal(request.data.get('confidence'), field='confidence', allow_null=False, minimum=0, maximum=1)
                if 'grams_per_ml' in request.data:
                    profile.grams_per_ml = _decimal(request.data.get('grams_per_ml'), field='grams_per_ml', minimum='0.00000001')
                for field in ('calories', 'protein_g', 'carbohydrate_g', 'fat_g', 'fiber_g', 'sugar_g', 'sodium_mg'):
                    if field in request.data:
                        setattr(profile, field, _decimal(request.data.get(field), field=field, minimum=0))
                if 'source_type' in request.data:
                    source_type = str(request.data.get('source_type') or '')
                    if source_type not in dict(FoodNutritionProfile.SOURCE_CHOICES):
                        raise ValueError('Unsupported source_type.')
                    profile.source_type = source_type
                if 'verified' in request.data:
                    profile.verified = bool(request.data.get('verified'))
                if 'is_default' in request.data:
                    is_default = bool(request.data.get('is_default'))
                    if is_default:
                        FoodNutritionProfile.objects.filter(food=profile.food, space=request.space, is_default=True).exclude(pk=profile.pk).update(is_default=False)
                    profile.is_default = is_default

                profile.save()
                response = _profile_payload(profile)
                record_agent_event(
                    request,
                    action='nutrition_profile.update',
                    target_type='FoodNutritionProfile',
                    target_id=profile.id,
                    before=before,
                    after=response,
                    response=response,
                    metadata={'food_id': profile.food_id},
                )
        except ValueError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            return Response({'error': True, 'msg': 'The idempotency key or default profile conflicts with an existing write.'}, status=status.HTTP_409_CONFLICT)
        return Response(response)


class AgentDraftNutritionView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request):
        items = request.data.get('ingredients')
        if not isinstance(items, list):
            return Response({'error': True, 'msg': 'ingredients must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(items) > 500:
            return Response({'error': True, 'msg': 'At most 500 ingredients can be evaluated at once.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(evaluate_draft(items, request.space))


class AgentAuditCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        try:
            limit = min(max(int(request.query_params.get('limit', 50)), 1), 200)
        except (TypeError, ValueError):
            limit = 50
        events = (AgentAuditEvent.objects
                  .filter(space=request.space, created_by=request.user)
                  .order_by('-created_at')[:limit])
        return Response([
            {
                'event_id': str(event.event_id),
                'client_id': event.client_id,
                'action': event.action,
                'target_type': event.target_type,
                'target_id': event.target_id,
                'request_id': event.request_id,
                'success': event.success,
                'error': event.error,
                'metadata': event.metadata,
                'created_at': event.created_at.isoformat(),
            }
            for event in events
        ])
