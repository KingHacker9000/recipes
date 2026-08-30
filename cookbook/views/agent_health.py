from rest_framework.response import Response
from rest_framework.views import APIView

from cookbook.helper.permission_helper import CustomIsUser, CustomTokenHasReadWriteScope


AGENT_PERMISSION = [CustomIsUser & CustomTokenHasReadWriteScope]
AGENT_API_VERSION = '2026-08-31.v4'


class AgentHealthView(APIView):
    permission_classes = AGENT_PERMISSION

    def get(self, request):
        return Response({
            'version': AGENT_API_VERSION,
            'space_id': request.space.id,
            'capabilities': {
                'recipes': [
                    'search', 'get', 'create', 'update', 'clone', 'nutrition_analyze',
                    'native_nutrition_properties', 'image_read', 'image_upload',
                    'exact_scale_preview', 'practical_scale_preview', 'recommend',
                    'pantry_check', 'substitution_context', 'variant_preview', 'variant_save',
                ],
                'foods': ['search', 'nutrition_profile_read', 'nutrition_profile_write', 'fdc_search', 'fdc_verify'],
                'nutrition': ['evaluate_draft', 'coverage', 'provenance', 'native_property_sync'],
                'pantry': ['locations', 'entries', 'adjust_delta', 'reconcile_preview', 'proposal_apply'],
                'shopping': ['lists', 'entry_create', 'entry_update', 'entry_delete'],
                'meal_plans': ['meal_types', 'list', 'create', 'update', 'delete'],
                'audit': ['list', 'idempotency'],
                'mcp': ['semantic_tools', 'stdio', 'authenticated_streamable_http'],
            },
        })
