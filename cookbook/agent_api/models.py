import uuid

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django_scopes import ScopedManager

from cookbook.models import Food, Recipe, Space


class AgentAuditEvent(models.Model):
    """Append-only audit record for writes performed through the Agent API."""

    event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    client_id = models.CharField(max_length=128, default='tandoor-agent-api')
    action = models.CharField(max_length=128)
    target_type = models.CharField(max_length=128, blank=True, default='')
    target_id = models.CharField(max_length=128, blank=True, default='')
    request_id = models.CharField(max_length=128, blank=True, default='')
    idempotency_key = models.CharField(max_length=256, blank=True, default='')
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    response = models.JSONField(default=dict, blank=True)
    success = models.BooleanField(default=True)
    error = models.TextField(blank=True, default='')

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='agent_audit_events')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ScopedManager(space='space')

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['space', 'created_at'], name='agent_audit_space_created_idx'),
            models.Index(fields=['created_by', 'created_at'], name='agent_audit_user_created_idx'),
            models.Index(fields=['space', 'action', 'created_at'], name='agent_audit_action_created_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['space', 'created_by', 'client_id', 'idempotency_key'],
                condition=~Q(idempotency_key=''),
                name='agent_audit_unique_idempotency_key',
            ),
        ]


class AgentProposal(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPLIED = 'applied'
    STATUS_REJECTED = 'rejected'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPLIED, 'Applied'),
        (STATUS_REJECTED, 'Rejected'),
        (STATUS_EXPIRED, 'Expired'),
    )

    proposal_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    proposal_type = models.CharField(max_length=128)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_PENDING)
    payload = models.JSONField(default=dict, blank=True)
    preview = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    revision_key = models.CharField(max_length=256, blank=True, default='')
    expires_at = models.DateTimeField(blank=True, null=True)
    applied_at = models.DateTimeField(blank=True, null=True)

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='agent_proposals')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScopedManager(space='space')

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['space', 'status', 'created_at'], name='agent_prop_space_status_idx'),
            models.Index(fields=['created_by', 'status', 'created_at'], name='agent_prop_user_status_idx'),
        ]


class FoodNutritionProfile(models.Model):
    """Nutrition facts for a specific food/product with explicit provenance.

    Values are expressed per ``basis_amount`` of ``basis_unit``. Known mass and
    volume units are converted deterministically by the nutrition service.
    Unknown/count units are only used when the ingredient unit matches exactly.
    """

    SOURCE_USER_LABEL = 'user_label'
    SOURCE_BRANDED = 'branded'
    SOURCE_REFERENCE = 'reference'
    SOURCE_ESTIMATED = 'estimated'
    SOURCE_AI = 'ai_estimate'
    SOURCE_MANUAL = 'manual'
    SOURCE_CHOICES = (
        (SOURCE_USER_LABEL, 'User verified nutrition label'),
        (SOURCE_BRANDED, 'Exact branded product'),
        (SOURCE_REFERENCE, 'Reference food database'),
        (SOURCE_ESTIMATED, 'Estimated comparable food'),
        (SOURCE_AI, 'AI estimate'),
        (SOURCE_MANUAL, 'Manual entry'),
    )

    food = models.ForeignKey(Food, on_delete=models.CASCADE, related_name='nutrition_profiles')
    label = models.CharField(max_length=256, blank=True, default='')
    brand = models.CharField(max_length=256, blank=True, default='')
    barcode = models.CharField(max_length=128, blank=True, default='')

    basis_amount = models.DecimalField(max_digits=16, decimal_places=6, default=100)
    basis_unit = models.CharField(max_length=64, default='g')
    grams_per_ml = models.DecimalField(max_digits=16, decimal_places=8, blank=True, null=True)

    calories = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    protein_g = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    carbohydrate_g = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    fat_g = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    fiber_g = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    sugar_g = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)
    sodium_mg = models.DecimalField(max_digits=16, decimal_places=6, blank=True, null=True)

    source_type = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_reference = models.CharField(max_length=2048, blank=True, default='')
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    verified = models.BooleanField(default=False)
    is_default = models.BooleanField(default=False)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, blank=True, null=True, related_name='food_nutrition_profiles')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ScopedManager(space='space')

    class Meta:
        ordering = ('food_id', '-is_default', '-verified', '-confidence', '-updated_at')
        indexes = [
            models.Index(fields=['space', 'food'], name='agent_nutr_food_idx'),
            models.Index(fields=['space', 'barcode'], name='agent_nutr_barcode_idx'),
            models.Index(fields=['space', 'source_type'], name='agent_nutr_source_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['space', 'food'],
                condition=Q(is_default=True),
                name='agent_one_default_nutrition_profile_per_food',
            ),
        ]


class RecipeVariantLink(models.Model):
    """Lineage metadata for ordinary Tandoor recipes created as variants."""

    recipe = models.OneToOneField(Recipe, on_delete=models.CASCADE, related_name='agent_variant_metadata')
    parent_recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='agent_variants')
    variant_type = models.CharField(max_length=128, blank=True, default='custom')
    constraints = models.JSONField(default=dict, blank=True)
    change_summary = models.JSONField(default=list, blank=True)
    original_macros = models.JSONField(default=dict, blank=True)
    variant_macros = models.JSONField(default=dict, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='agent_recipe_variants')
    space = models.ForeignKey(Space, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ScopedManager(space='space')

    class Meta:
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=['space', 'parent_recipe', 'created_at'], name='agent_variant_parent_idx'),
        ]
