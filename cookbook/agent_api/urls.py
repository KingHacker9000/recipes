from django.urls import path

from cookbook.views import agent_api


urlpatterns = [
    path('health/', agent_api.AgentHealthView.as_view(), name='api_agent_health'),
    path('recipes/', agent_api.AgentRecipeCollectionView.as_view(), name='api_agent_recipes'),
    path('recipes/<int:pk>/', agent_api.AgentRecipeDetailView.as_view(), name='api_agent_recipe_detail'),
    path('recipes/<int:pk>/nutrition/', agent_api.AgentRecipeNutritionView.as_view(), name='api_agent_recipe_nutrition'),
    path('recipes/<int:pk>/scale-preview/', agent_api.AgentRecipeScalePreviewView.as_view(), name='api_agent_recipe_scale_preview'),
    path('foods/', agent_api.AgentFoodSearchView.as_view(), name='api_agent_foods'),
    path('nutrition-profiles/', agent_api.AgentFoodNutritionProfileCollectionView.as_view(), name='api_agent_nutrition_profiles'),
    path('nutrition-profiles/<int:pk>/', agent_api.AgentFoodNutritionProfileDetailView.as_view(), name='api_agent_nutrition_profile_detail'),
    path('nutrition/evaluate-draft/', agent_api.AgentDraftNutritionView.as_view(), name='api_agent_nutrition_evaluate_draft'),
    path('audit/', agent_api.AgentAuditCollectionView.as_view(), name='api_agent_audit'),
]
