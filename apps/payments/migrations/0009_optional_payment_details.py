"""PAY-UX-01: the paying phone and the transaction reference become optional for the member.

Members may request verification with no details at all. Every rule for APPROVED claims stays as
it was: an approved claim always has a valid reference, unique per provider among open and approved
claims, matching its finalized ledger posting and its membership activation.

* Check constraints: "" is allowed for the phone, and for the reference while the claim is not
  approved. The displayed reference and its key are given or absent together.
* Single use: claims without a reference are not keyed (many can wait at once).
* Claim update guard (MariaDB and PostgreSQL): submitted details stay frozen. The one exception is
  a claim that has no reference: the update that approves it records the reference the reviewer
  found in the provider records, once. After that the claim is final, as before.
"""
from importlib import import_module

from django.conf import settings
from django.db import migrations, models

from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, changed, privilege_error

base = import_module("apps.payments.migrations.0005_manual_payment_integrity")
guards = import_module("apps.payments.migrations.0007_manual_method_guards")

REFERENCE = ("transaction_reference", "reference_key")
FROZEN = tuple(column for column in base.CLAIM_SUBMISSION if column not in REFERENCE)
REFUSED = privilege_error("submitted payment details cannot change")

# MariaDB: the current claim update trigger (as 0007 installed it), with the freeze clause narrowed.
CLAIM_UPDATE_0007 = next(statement for statement in guards.maria
                         if statement.startswith("CREATE TRIGGER payments_manual_claim_update "))
FREEZE_0005 = f"IF {changed(*base.CLAIM_SUBMISSION)} THEN\n        {REFUSED}\n    END IF;"
FREEZE_0009 = (f"IF {changed(*FROZEN)}\n"
               f"       OR ({changed(*REFERENCE)}\n"
               "           AND NOT (BINARY OLD.reference_key = '' AND BINARY OLD.transaction_reference = ''\n"
               "                    AND BINARY NEW.status = 'APPROVED')) THEN\n"
               f"        {REFUSED}\n    END IF;")
assert CLAIM_UPDATE_0007.count(FREEZE_0005) == 1, "0007 claim update trigger changed: review 0009"
MARIADB_FORWARD = ["DROP TRIGGER payments_manual_claim_update", CLAIM_UPDATE_0007.replace(FREEZE_0005, FREEZE_0009)]
MARIADB_REVERSE = ["DROP TRIGGER payments_manual_claim_update", CLAIM_UPDATE_0007]

# PostgreSQL: the same change to the claim guard function (as 0007 replaced it).
PG_FREEZE_0005 = ("       OR NEW.payer_phone <> OLD.payer_phone OR NEW.transaction_reference <> OLD.transaction_reference\n"
                  "       OR NEW.reference_key <> OLD.reference_key OR NEW.paid_on IS DISTINCT FROM OLD.paid_on\n")
PG_FREEZE_0009 = ("       OR NEW.payer_phone <> OLD.payer_phone OR NEW.paid_on IS DISTINCT FROM OLD.paid_on\n"
                  "       OR ((NEW.transaction_reference <> OLD.transaction_reference"
                  " OR NEW.reference_key <> OLD.reference_key)\n"
                  "           AND NOT (OLD.reference_key = '' AND OLD.transaction_reference = ''"
                  " AND NEW.status = 'APPROVED'))\n")
assert guards.claim_pg.count(PG_FREEZE_0005) == 1, "0007 claim guard function changed: review 0009"
POSTGRES_FORWARD = guards.claim_pg.replace(PG_FREEZE_0005, PG_FREEZE_0009)
POSTGRES_REVERSE = guards.claim_pg


class Migration(migrations.Migration):

    dependencies = [
        ('memberships', '0004_administrative_extension'),
        ('payments', '0008_manual_payment_evidence'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='manualmembershippayment',
            name='manual_payment_reference_key_format',
        ),
        migrations.RemoveConstraint(
            model_name='manualmembershippayment',
            name='manual_payment_phone_e164',
        ),
        migrations.RemoveConstraint(
            model_name='manualmembershippayment',
            name='manual_payment_reference_single_use',
        ),
        migrations.AlterField(
            model_name='manualmembershippayment',
            name='payer_phone',
            field=models.CharField(blank=True, max_length=16, verbose_name='paying phone number'),
        ),
        migrations.AlterField(
            model_name='manualmembershippayment',
            name='reference_key',
            field=models.CharField(blank=True, editable=False, max_length=40, verbose_name='reference key'),
        ),
        migrations.AlterField(
            model_name='manualmembershippayment',
            name='transaction_reference',
            field=models.CharField(blank=True, max_length=40, verbose_name='transaction reference'),
        ),
        migrations.AddConstraint(
            model_name='manualmembershippayment',
            constraint=models.CheckConstraint(condition=models.Q(('reference_key__regex', '^[A-Z0-9]{6,40}$'), models.Q(('reference_key', ''), models.Q(('status', 'APPROVED'), _negated=True)), _connector='OR'), name='manual_payment_reference_key_format'),
        ),
        migrations.AddConstraint(
            model_name='manualmembershippayment',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('reference_key', ''), ('transaction_reference', '')), models.Q(models.Q(('reference_key', ''), _negated=True), models.Q(('transaction_reference', ''), _negated=True)), _connector='OR'), name='manual_payment_reference_pair'),
        ),
        migrations.AddConstraint(
            model_name='manualmembershippayment',
            constraint=models.CheckConstraint(condition=models.Q(('payer_phone__regex', '^\\+[1-9][0-9]{7,14}$'), ('payer_phone', ''), _connector='OR'), name='manual_payment_phone_e164'),
        ),
        migrations.AddConstraint(
            model_name='manualmembershippayment',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ('PENDING_VERIFICATION', 'APPROVED')), models.Q(('reference_key', ''), _negated=True)), fields=('method', 'reference_key'), name='manual_payment_reference_single_use'),
        ),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
        PostgreSQLRunSQL(POSTGRES_FORWARD, POSTGRES_REVERSE),
    ]
