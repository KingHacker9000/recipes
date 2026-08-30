from django.urls import path

from cookbook.views import agent_api, agent_pantry


urlpatterns = [
    path('health/', agent_api.AgentHealthView.as_view(), name='api_agent_health'),
    path('recipes/', agent_api.AgentRecipeCollectionView.as_view(), name='api_agent_recipes'),
    path('recipes/<int:pk>/', agent_api.AgentRecipeDetailView.as_view(), name='api_agent_recipe_detail'),
    path('recipes/<int:pk>/clone/', agent_api.AgentRecipeCloneView.as_view(), name='api_agent_recipe_clone'),
    path('recipes/<int:pk>/nutrition/', agent_api.AgentRecipeNutritionView.as_view(), name='api_agent_recipe_nutrition'),
    path('recipes/<int:pk>/scale-preview/', agent_api.AgentRecipeScalePreviewView.as_view(), name='api_agent_recipe_scale_preview'),
    path('recipes/<int:pk>/variant-preview/', agent_api.AgentRecipeVariantPreviewView.as_view(), name='api_agent_recipe_variant_preview'),
    path('recipes/<int:pk>/save-variant/', agent_api.AgentRecipeSaveVariantView.as_view(), name='api_agent_recipe_save_variant'),
    path('recipes/<int:pk>/pantry-check/', agent_pantry.AgentRecipePantryCheckView.as_view(), name='api_agent_recipe_pantry_check'),
    path('foods/', agent_api.AgentFoodSearchView.as_view(), name='api_agent_foods'),
    path('nutrition-profiles/', agent_api.AgentFoodNutritionProfileCollectionView.as_view(), name='api_agent_nutrition_profiles'),
    path('nutrition-profiles/<int:pk>/', agent_api.AgentFoodNutritionProfileDetailView.as_view(), name='api_agent_nutrition_profile_detail'),
    path('nutrition/evaluate-draft/', agent_api.AgentDraftNutritionView.as_view(), name='api_agent_nutrition_evaluate_draft'),
    path('pantry/locations/', agent_pantry.AgentPantryLocationCollectionView.as_view(), name='api_agent_pantry_locations'),
    path('pantry/entries/', agent_pantry.AgentPantryEntryCollectionView.as_view(), name='api_agent_pantry_entries'),
    path('pantry/reconcile-preview/', agent_pantry.AgentPantryReconcilePreviewView.as_view(), name='api_agent_pantry_reconcile_preview'),
    path('proposals/<uuid:proposal_id>/', agent_pantry.AgentProposalDetailView.as_view(), name='api_agent_proposal_detail'),
    path('proposals/<uuid:proposal_id>/apply/', agent_pantry.AgentProposalApplyView.as_view(), name='api_agent_proposal_apply'),
    path('audit/', agent_api.AgentAuditCollectionView.as_view(), name='api_agent_audit'),
]
