"""Enable btree_gist (a trusted PostgreSQL extension) for the subscription
exclusion constraint that prevents overlapping membership periods."""

from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = []

    operations = [BtreeGistExtension()]
