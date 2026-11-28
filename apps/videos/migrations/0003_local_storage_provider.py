"""
OPS-01: allow the "LOCAL" video provider (files kept on the hosting account; apps.videos.providers.local).

Only the provider check constraint and the field choices change: no column is added, no row is touched,
and the existing video guards (no deletion, archived is final, a provider id never changes) are unchanged.
Existing Bunny and demo videos keep their provider.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('videos', '0002_integrity'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='video',
            name='video_provider_valid',
        ),
        migrations.AlterField(
            model_name='video',
            name='provider',
            field=models.CharField(choices=[('BUNNY', 'Bunny Stream'), ('DEMO', 'Demo sample (no real media)'), ('LOCAL', 'Local storage')], max_length=10, verbose_name='provider'),
        ),
        migrations.AddConstraint(
            model_name='video',
            constraint=models.CheckConstraint(condition=models.Q(('provider__in', ['BUNNY', 'DEMO', 'LOCAL'])), name='video_provider_valid'),
        ),
    ]
