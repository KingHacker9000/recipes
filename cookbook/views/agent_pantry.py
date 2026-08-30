from django.db import IntegrityError
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.agent_api.audit import find_idempotent_replay, record_agent_event
from cookbook.agent_api.models import AgentProposal, FoodNutritionProfile
from cookbook.agent_api.nutrition import profile_dict
from cookbook.agent_api.pantry import (
    AgentPantryInputError,
    apply_reconcile_proposal,
    check_recipe_against_pantry,
    create_reconcile_proposal,
    entry_payload,
    inventory_queryset,
    location_payload,
    proposal_payload,
)
from cookbook.helper.permission_helper import CustomIsUser, CustomRecipePermission, CustomTokenHasReadWriteScope
from cookbook.models import InventoryLocation, Recipe


AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]


def _recipe_for_request(view, request, pk):
    recipe = (Recipe.objects
              .filter(space=request.space, pk=pk)
              .filter(Q(private=False) | Q(created_by=request.user) | Q(shared=request.user))
              .prefetch_related('shared', 'steps__ingredients__food', 'steps__ingredients__unit')
              .distinct()
              .first())
    if recipe is None:
        return None
    if not CustomRecipePermission().has_object_permission(request, view, recipe):
        return None
    return recipe


def _replay_response(request):
    replay = find_idempotent_replay(request)
    if replay is None:
        return None
    response = dict(replay.response or {})
    response['idempotent_replay'] = True
    return Response(response, status=status.HTTP_200_OK)


def _input_error(exc, conflict=False):
    return Response(
        {'error': True, 'msg': str(exc)},
        status=status.HTTP_409_CONFLICT if conflict else status.HTTP_400_BAD_REQUEST,
    )


class AgentPantryLocationCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        locations = (InventoryLocation.objects
                     .filter(space=request.space)
                     .select_related('household')
                     .order_by('name', 'id'))
        return Response([location_payload(location) for location in locations])


class AgentPantryEntryCollectionView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        queryset = inventory_queryset(request.space).order_by('expires', 'id')
        if str(request.query_params.get('include_empty') or '').lower() not in ('1', 'true', 'yes'):
            queryset = queryset.filter(amount__gt=0)
        if request.query_params.get('location_id'):
            queryset = queryset.filter(inventory_location_id=request.query_params['location_id'])
        if request.query_params.get('food_id'):
            queryset = queryset.filter(food_id=request.query_params['food_id'])
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(food__name__icontains=query)
                | Q(note__icontains=query)
                | Q(code__icontains=query)
                | Q(inventory_location__name__icontains=query)
            )
        try:
            limit = min(max(int(request.query_params.get('limit', 200)), 1), 500)
        except (TypeError, ValueError):
            limit = 200

        entries = []
        for entry in queryset[:limit]:
            value = entry_payload(entry)
            if entry.food_id:
                profile = (FoodNutritionProfile.objects
                           .filter(space=request.space, food_id=entry.food_id)
                           .order_by('-is_default', '-verified', '-confidence', '-updated_at')
                           .first())
                value['nutrition_profile'] = profile_dict(profile)
            else:
                value['nutrition_profile'] = None
            entries.append(value)
        return Response(entries)


class AgentRecipePantryCheckView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, pk):
        recipe = _recipe_for_request(self, request, pk)
        if recipe is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(check_recipe_against_pantry(recipe, request.space))


class AgentPantryReconcilePreviewView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request):
        replay = _replay_response(request)
        if replay:
            return replay
        observations = request.data.get('observations')
        mode = str(request.data.get('mode') or 'augment').strip().lower()
        default_location_id = request.data.get('default_location_id')
        try:
            proposal = create_reconcile_proposal(
                request,
                observations,
                mode=mode,
                default_location_id=default_location_id,
            )
            response = proposal_payload(proposal)
            record_agent_event(
                request,
                action='pantry.reconcile.preview',
                target_type='AgentProposal',
                target_id=proposal.proposal_id,
                after={'status': proposal.status, 'summary': proposal.preview.get('summary', {})},
                response=response,
                metadata={'mode': mode, 'location_ids': proposal.payload.get('location_ids', [])},
            )
        except AgentPantryInputError as exc:
            return _input_error(exc)
        except IntegrityError:
            return Response(
                {'error': True, 'msg': 'The idempotency key conflicts with an existing write.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(response, status=status.HTTP_201_CREATED)


class AgentProposalDetailView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request, proposal_id):
        proposal = (AgentProposal.objects
                    .filter(proposal_id=proposal_id, space=request.space, created_by=request.user)
                    .first())
        if proposal is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(proposal_payload(proposal))


class AgentProposalApplyView(APIView):
    permission_classes = AGENT_PERMISSION

    def post(self, request, proposal_id):
        replay = _replay_response(request)
        if replay:
            return replay
        proposal = (AgentProposal.objects
                    .filter(proposal_id=proposal_id, space=request.space, created_by=request.user)
                    .first())
        if proposal is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        before = proposal_payload(proposal)
        try:
            proposal = apply_reconcile_proposal(
                request,
                proposal,
                confirmed=request.data.get('confirmed') is True,
            )
            response = proposal_payload(proposal)
            record_agent_event(
                request,
                action='pantry.reconcile.apply',
                target_type='AgentProposal',
                target_id=proposal.proposal_id,
                before=before,
                after=response,
                response=response,
                metadata={
                    'mode': proposal.payload.get('mode'),
                    'applied_count': (proposal.result or {}).get('applied_count', 0),
                },
            )
        except AgentPantryInputError as exc:
            message = str(exc).lower()
            conflict = any(term in message for term in ('changed since', 'expired', 'already ', 'no longer pending', 'disappeared'))
            return _input_error(exc, conflict=conflict)
        except IntegrityError:
            return Response(
                {'error': True, 'msg': 'The idempotency key conflicts with an existing write.'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(response)
