# Generated for Tandoor Agent API / nutrition foundation.

import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('cookbook', '0243_socialimportjob'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AgentAuditEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('client_id', models.CharField(default='tandoor-agent-api', max_length=128)),
                ('action', models.CharField(max_length=128)),
                ('target_type', models.CharField(blank=True, default='', max_length=128)),
                ('target_id', models.CharField(blank=True, default='', max_length=128)),
                ('request_id', models.CharField(blank=True, default='', max_length=128)),
                ('idempotency_key', models.CharField(blank=True, default='', max_length=256)),
                ('before', models.JSONField(blank=True, default=dict)),
                ('after', models.JSONField(blank=True, default=dict)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('response', models.JSONField(blank=True, default=dict)),
                ('success', models.BooleanField(default=True)),
                ('error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_audit_events', to=settings.AUTH_USER_MODEL)),
                ('space', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cookbook.space')),
            ],
            options={
                'ordering': ('-created_at',),
                'indexes': [
                    models.Index(fields=['space', 'created_at'], name='agent_audit_space_created_idx'),
                    models.Index(fields=['created_by', 'created_at'], name='agent_audit_user_created_idx'),
                    models.Index(fields=['space', 'action', 'created_at'], name='agent_audit_action_created_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(condition=~models.Q(idempotency_key=''), fields=('space', 'created_by', 'client_id', 'idempotency_key'), name='agent_audit_unique_idempotency_key'),
                ],
            },
        ),
        migrations.CreateModel(
            name='AgentProposal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('proposal_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('proposal_type', models.CharField(max_length=128)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('applied', 'Applied'), ('rejected', 'Rejected'), ('expired', 'Expired')], default='pending', max_length=32)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('preview', models.JSONField(blank=True, default=dict)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('revision_key', models.CharField(blank=True, default='', max_length=256)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_proposals', to=settings.AUTH_USER_MODEL)),
                ('space', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cookbook.space')),
            ],
            options={
                'ordering': ('-created_at',),
                'indexes': [
                    models.Index(fields=['space', 'status', 'created_at'], name='agent_prop_space_status_idx'),
                    models.Index(fields=['created_by', 'status', 'created_at'], name='agent_prop_user_status_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='FoodNutritionProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(blank=True, default='', max_length=256)),
                ('brand', models.CharField(blank=True, default='', max_length=256)),
                ('barcode', models.CharField(blank=True, default='', max_length=128)),
                ('basis_amount', models.DecimalField(decimal_places=6, default=100, max_digits=16)),
                ('basis_unit', models.CharField(default='g', max_length=64)),
                ('grams_per_ml', models.DecimalField(blank=True, decimal_places=8, max_digits=16, null=True)),
                ('calories', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('protein_g', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('carbohydrate_g', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('fat_g', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('fiber_g', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('sugar_g', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('sodium_mg', models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True)),
                ('source_type', models.CharField(choices=[('user_label', 'User verified nutrition label'), ('branded', 'Exact branded product'), ('reference', 'Reference food database'), ('estimated', 'Estimated comparable food'), ('ai_estimate', 'AI estimate'), ('manual', 'Manual entry')], default='manual', max_length=32)),
                ('source_reference', models.CharField(blank=True, default='', max_length=2048)),
                ('confidence', models.DecimalField(decimal_places=4, default=1, max_digits=5, validators=[MinValueValidator(0), MaxValueValidator(1)])),
                ('verified', models.BooleanField(default=False)),
                ('is_default', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='food_nutrition_profiles', to=settings.AUTH_USER_MODEL)),
                ('food', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='nutrition_profiles', to='cookbook.food')),
                ('space', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cookbook.space')),
            ],
            options={
                'ordering': ('food_id', '-is_default', '-verified', '-confidence', '-updated_at'),
                'indexes': [
                    models.Index(fields=['space', 'food'], name='agent_nutr_food_idx'),
                    models.Index(fields=['space', 'barcode'], name='agent_nutr_barcode_idx'),
                    models.Index(fields=['space', 'source_type'], name='agent_nutr_source_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(condition=models.Q(is_default=True), fields=('space', 'food'), name='agent_one_default_nutrition_profile_per_food'),
                ],
            },
        ),
        migrations.CreateModel(
            name='RecipeVariantLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('variant_type', models.CharField(blank=True, default='custom', max_length=128)),
                ('constraints', models.JSONField(blank=True, default=dict)),
                ('change_summary', models.JSONField(blank=True, default=list)),
                ('original_macros', models.JSONField(blank=True, default=dict)),
                ('variant_macros', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_recipe_variants', to=settings.AUTH_USER_MODEL)),
                ('parent_recipe', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_variants', to='cookbook.recipe')),
                ('recipe', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='agent_variant_metadata', to='cookbook.recipe')),
                ('space', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cookbook.space')),
            ],
            options={
                'ordering': ('-created_at',),
                'indexes': [
                    models.Index(fields=['space', 'parent_recipe', 'created_at'], name='agent_variant_parent_idx'),
                ],
            },
        ),
    ]
