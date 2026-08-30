from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.agent_api.audit import find_idempotent_replay, record_agent_event
from cookbook.agent_api.images import AgentRecipeImageError, normalize_recipe_image, recipe_image_payload
from cookbook.helper.permission_helper import CustomIsUser, CustomRecipePermission, CustomTokenHasReadWriteScope
from cookbook.models import Recipe


AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]


def _replay(request):
    event = find_idempotent_replay(request)
    if event is None:
        return None
    payload = dict(event.response or {})
    payload['idempotent_replay'] = True
    return Response(payload, status=status.HTTP_200_OK)


def _recipe(view, request, pk):
    recipe = Recipe.objects.filter(pk=pk, space=request.space).first()
    if recipe is None:
        return None
    if not CustomRecipePermission().has_object_permission(request, view, recipe):
        return None
    return recipe


class AgentRecipeImageView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request, pk):
        recipe = _recipe(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(recipe_image_payload(recipe))

    def post(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay

        expected_updated_at = str(request.data.get('expected_updated_at') or '').strip()
        if not expected_updated_at:
            return Response(
                {'error': True, 'msg': 'expected_updated_at is required for recipe image updates.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            normalized = normalize_recipe_image(
                request.data.get('image_base64'),
                content_type=request.data.get('content_type') or '',
            )
        except AgentRecipeImageError as exc:
            return Response({'error': True, 'msg': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        new_name = ''
        storage = None
        old_name = ''
        try:
            with transaction.atomic():
                recipe = Recipe.objects.select_for_update().filter(pk=pk, space=request.space).first()
                if recipe is None or not CustomRecipePermission().has_object_permission(request, self, recipe):
                    return Response(status=status.HTTP_404_NOT_FOUND)
                if not recipe.updated_at or recipe.updated_at.isoformat() != expected_updated_at:
                    return Response(
                        {
                            'error': True,
                            'msg': 'Recipe changed since it was read.',
                            'current': recipe_image_payload(recipe),
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                before = recipe_image_payload(recipe)
                old_name = recipe.image.name if recipe.image else ''
                storage = recipe.image.storage
                recipe.image.save(normalized['filename'], normalized['content'], save=True)
                new_name = recipe.image.name
                response = {
                    **recipe_image_payload(recipe),
                    'content_type': normalized['content_type'],
                    'width': normalized['width'],
                    'height': normalized['height'],
                    'size_bytes': normalized['size_bytes'],
                }
                record_agent_event(
                    request,
                    action='recipe.image.update',
                    target_type='Recipe',
                    target_id=recipe.id,
                    before=before,
                    after=response,
                    response=response,
                    metadata={
                        'content_type': normalized['content_type'],
                        'width': normalized['width'],
                        'height': normalized['height'],
                        'size_bytes': normalized['size_bytes'],
                    },
                )
                if old_name and old_name != new_name:
                    transaction.on_commit(lambda: storage.delete(old_name))
        except IntegrityError:
            if storage is not None and new_name:
                try:
                    storage.delete(new_name)
                except OSError:
                    pass
            return Response(
                {'error': True, 'msg': 'The idempotency key conflicts with an existing write.'},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(response)
