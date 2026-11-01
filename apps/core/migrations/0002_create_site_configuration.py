from django.db import migrations


def create_site_configuration(apps, schema_editor):
    SiteConfiguration = apps.get_model("core", "SiteConfiguration")
    # Field defaults apply; financial features have no fields and stay locked off.
    SiteConfiguration.objects.get_or_create(id=1)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        # Reverse is a no-op: never delete live configuration on rollback.
        migrations.RunPython(create_site_configuration, migrations.RunPython.noop),
    ]
