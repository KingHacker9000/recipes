from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response

from cookbook.agent_api.audit import find_idempotent_replay, record_agent_event
from cookbook.agent_api.household import (
    AgentHouseholdInputError,
    accessible_meal_plan_queryset,
    meal_plan_payload,
    update_meal_plan,
)
from cookbook.models import MealPlan
from cookbook.views.agent_actions import AgentMealPlanDetailView


def _replay(request):
    event = find_idempotent_replay(request)
    if event is None:
        return None
    payload = dict(event.response or {})
    payload['idempotent_replay'] = True
    return Response(payload, status=status.HTTP_200_OK)


def _error(exc, *, conflict=False):
    return Response(
        {'error': True, 'msg': str(exc)},
        status=status.HTTP_409_CONFLICT if conflict else status.HTTP_400_BAD_REQUEST,
    )


class AgentMealPlanMutableDetailView(AgentMealPlanDetailView):
    """Add optimistic PATCH support while preserving confirmed DELETE behavior."""

    def patch(self, request, pk):
        replay = _replay(request)
        if replay:
            return replay

        current = (accessible_meal_plan_queryset(request)
                   .filter(pk=pk)
                   .select_related('recipe', 'meal_type')
                   .first())
        if current is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        before = meal_plan_payload(current)

        try:
            with transaction.atomic():
                locked = (MealPlan.objects
                          .select_for_update()
                          .filter(pk=pk, space=request.space)
                          .select_related('recipe', 'meal_type')
                          .first())
                if locked is None or not accessible_meal_plan_queryset(request).filter(pk=locked.pk).exists():
                    return Response(status=status.HTTP_404_NOT_FOUND)
                update_meal_plan(request, locked, request.data)
                updated = (accessible_meal_plan_queryset(request)
                           .filter(pk=pk)
                           .select_related('recipe', 'meal_type')
                           .first())
                if updated is None:
                    return Response(status=status.HTTP_404_NOT_FOUND)
                response = meal_plan_payload(updated)
                record_agent_event(
                    request,
                    action='meal_plan.update',
                    target_type='MealPlan',
                    target_id=pk,
                    before=before,
                    after=response,
                    response=response,
                )
        except AgentHouseholdInputError as exc:
            return _error(exc, conflict='changed since' in str(exc).lower())
        except IntegrityError:
            return Response(
                {'error': True, 'msg': 'The idempotency key conflicts with an existing write.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(response)
