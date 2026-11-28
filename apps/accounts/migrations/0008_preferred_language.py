"""ACC-01: a saved language choice per account (empty for every existing account: no choice yet)."""

import apps.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_phone_verification_challenge"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="preferred_language",
            field=models.CharField(
                blank=True, default="", help_text="Chosen with the language switch. Empty: no choice yet.",
                max_length=8, validators=[apps.core.validators.validate_language],
                verbose_name="preferred language"),
        ),
    ]
