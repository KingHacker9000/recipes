from django.urls import path

from cookbook.views import agent_actions, agent_api, agent_extended, agent_pantry


urlpatterns = [
    path('health/', agent_api.AgentHealthView.as_view(), name='api_agent_health'),
    path('recipes/', agent_api.AgentRecipeCollectionView.as_view(), name='api_agent_recipes'),
    path('recipes/recommend/', agent_extended.AgentRecipeRecommendationView.as_view(), name='api_agent_recipe_recommend'),
    path('recipes/<int:pk>/', agent_api.AgentRecipeDetailView.as_view(), name='api_agent_recipe_detail'),
    path('recipes/<int:pk>/clone/', agent_api.AgentRecipeCloneView.as_view(), name='api_agent_recipe_clone'),
    path('recipes/<int:pk>/nutrition/', agent_api.AgentRecipeNutritionView.as_view(), name='api_agent_recipe_nutrition'),
    path('recipes/<int:pk>/scale-preview/', agent_api.AgentRecipeScalePreviewView.as_view(), name='api_agent_recipe_scale_preview'),
    path('recipes/<int:pk>/practical-scale-preview/', agent_extended.AgentPracticalScaleView.as_view(), name='api_agent_recipe_practical_scale'),
    path('recipes/<int:pk>/variant-preview/', agent_api.AgentRecipeVariantPreviewView.as_view(), name='api_agent_recipe_variant_preview'),
    path('recipes/<int:pk>/save-variant/', agent_api.AgentRecipeSaveVariantView.as_view(), name='api_agent_recipe_save_variant'),
    path('recipes/<int:pk>/pantry-check/', agent_pantry.AgentRecipePantryCheckView.as_view(), name='api_agent_recipe_pantry_check'),
    path('recipes/<int:pk>/substitution-context/', agent_extended.AgentRecipeSubstitutionContextView.as_view(), name='api_agent_recipe_substitution_context'),
    path('foods/', agent_api.AgentFoodSearchView.as_view(), name='api_agent_foods'),
    path('foods/<int:pk>/nutrition/fdc/verify/', agent_actions.AgentFoodFdcVerifyView.as_view(), name='api_agent_food_fdc_verify'),
    path('nutrition-profiles/', agent_api.AgentFoodNutritionProfileCollectionView.as_view(), name='api_agent_nutrition_profiles'),
    path('nutrition-profiles/<int:pk>/', agent_api.AgentFoodNutritionProfileDetailView.as_view(), name='api_agent_nutrition_profile_detail'),
    path('nutrition/evaluate-draft/', agent_api.AgentDraftNutritionView.as_view(), name='api_agent_nutrition_evaluate_draft'),
    path('nutrition/fdc/search/', agent_actions.AgentFdcSearchView.as_view(), name='api_agent_fdc_search'),
    path('pantry/locations/', agent_pantry.AgentPantryLocationCollectionView.as_view(), name='api_agent_pantry_locations'),
    path('pantry/entries/', agent_pantry.AgentPantryEntryCollectionView.as_view(), name='api_agent_pantry_entries'),
    path('pantry/adjust/', agent_actions.AgentPantryAdjustView.as_view(), name='api_agent_pantry_adjust'),
    path('pantry/reconcile-preview/', agent_pantry.AgentPantryReconcilePreviewView.as_view(), name='api_agent_pantry_reconcile_preview'),
    path('shopping/lists/', agent_actions.AgentShoppingListCollectionView.as_view(), name='api_agent_shopping_lists'),
    path('shopping/entries/', agent_actions.AgentShoppingEntryCollectionView.as_view(), name='api_agent_shopping_entries'),
    path('shopping/entries/<int:pk>/', agent_actions.AgentShoppingEntryDetailView.as_view(), name='api_agent_shopping_entry_detail'),
    path('meal-types/', agent_actions.AgentMealTypeCollectionView.as_view(), name='api_agent_meal_types'),
    path('meal-plans/', agent_actions.AgentMealPlanCollectionView.as_view(), name='api_agent_meal_plans'),
    path('meal-plans/<int:pk>/', agent_actions.AgentMealPlanDetailView.as_view(), name='api_agent_meal_plan_detail'),
    path('proposals/<uuid:proposal_id>/', agent_pantry.AgentProposalDetailView.as_view(), name='api_agent_proposal_detail'),
    path('proposals/<uuid:proposal_id>/apply/', agent_pantry.AgentProposalApplyView.as_view(), name='api_agent_proposal_apply'),
    path('audit/', agent_api.AgentAuditCollectionView.as_view(), name='api_agent_audit'),
]
