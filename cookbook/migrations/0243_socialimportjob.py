# Generated for Social Recipe Inbox MVP.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('cookbook', '0242_space_household_setup_completed'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SocialImportJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_url', models.URLField(max_length=2048)),
                ('canonical_url', models.URLField(blank=True, default='', max_length=2048)),
                ('platform', models.CharField(choices=[('tiktok', 'TikTok'), ('instagram', 'Instagram'), ('youtube', 'YouTube')], max_length=32)),
                ('external_id', models.CharField(blank=True, default='', max_length=256)),
                ('creator', models.CharField(blank=True, default='', max_length=256)),
                ('caption', models.TextField(blank=True, default='')),
                ('thumbnail_url', models.URLField(blank=True, default='', max_length=2048)),
                ('transcript', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('acquiring', 'Acquiring media'), ('extracting', 'Extracting recipe'), ('ready', 'Ready for review'), ('saving', 'Saving'), ('saved', 'Saved'), ('failed', 'Failed')], default='queued', max_length=32)),
                ('extraction', models.JSONField(blank=True, default=dict)),
                ('confidence', models.DecimalField(blank=True, decimal_places=4, max_digits=5, null=True)),
                ('error', models.TextField(blank=True, default='')),
                ('retry_count', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('ai_provider', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='cookbook.aiprovider')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('recipe', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='social_import_jobs', to='cookbook.recipe')),
                ('space', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='cookbook.space')),
            ],
            options={'ordering': ('-created_at',)},
        ),
        migrations.AddIndex(
            model_name='socialimportjob',
            index=models.Index(fields=['space', 'status', 'created_at'], name='cookbook_so_space_i_8564c8_idx'),
        ),
        migrations.AddIndex(
            model_name='socialimportjob',
            index=models.Index(fields=['created_by', 'status', 'created_at'], name='cookbook_so_created_9541ff_idx'),
        ),
    ]
