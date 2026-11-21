"""Rehearse the manual-method upgrade using only a fresh, distinct MariaDB test_* database.

Refuses an existing test database. Creates fictional Airtel records on the accepted
pre-feature schema, applies both new migrations, verifies legacy behavior as the
restricted login, and drops only the disposable database it created.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    import os
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.conf import settings
    from django.db import connections, transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.core.management.sql import emit_post_migrate_signal
    from django.test import override_settings
    from django.utils import timezone
    from apps.accounts.tests.helpers import make_user, User
    from apps.memberships import services as memberships
    from apps.memberships.tests.helpers import enable_memberships
    from apps.payments import manual
    from apps.payments.models import ManualMembershipPayment, ManualPaymentInstruction
    from apps.wallets import services as ledger
    from apps.wallets.models import JournalEntry
    from apps.core.mariadb_guards import guard_findings

    owner = connections["default"]
    original = owner.settings_dict["NAME"]
    test_name = owner.settings_dict["TEST"]["NAME"]
    assert owner.vendor == "mysql"
    assert test_name.startswith("test_") and test_name != original and test_name != "test_clipcash_db"
    created = False
    try:
        with owner.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [test_name])
            assert cursor.fetchone()[0] == 0, "Refusing to replace an existing database"
            cursor.execute(f"CREATE DATABASE {owner.ops.quote_name(test_name)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            created = True
        owner.close()
        owner.settings_dict["NAME"] = test_name
        executor = MigrationExecutor(owner)
        latest = executor.loader.graph.leaf_nodes()
        before = [(app, "0005_manual_payment_integrity") if app == "payments" else (app, name)
                  for app, name in latest]
        executor.migrate(before)
        emit_post_migrate_signal(0, False, "default")
        historical = executor.loader.project_state(before).apps
        OldInstruction = historical.get_model("payments", "ManualPaymentInstruction")
        OldClaim = historical.get_model("payments", "ManualMembershipPayment")
        with override_settings(MANUAL_MEMBERSHIP_PAYMENTS_ENABLED=True, RESTRICTED_DEMO_MODE=False,
                               PAYMENTS_MODE="disabled", PAYMENTS_SANDBOX_ALLOWED=False,
                               PUBLIC_REGISTRATION_APPROVED=True,
                               PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
            enable_memberships()
            root = make_user("upgrade-admin@example.com", role=User.Role.SUPER_ADMIN)
            member1 = make_user("upgrade-approved@example.com")
            member2 = make_user("upgrade-pending@example.com")
            plan = memberships.create_plan(root, code="upgrade-test", name="Upgrade Test", price_amount=5000,
                                            duration_days=30, entitlements=["videos.standard"])
            instruction = OldInstruction.objects.create(method="AIRTEL_LIPA", business_number="123456",
                                                         recipient_name="TEST ONLY LEGACY", instructions="Test records only",
                                                         is_active=True, created_by_id=root.pk)
            def legacy_claim(member, reference):
                pending = memberships.choose_plan(member, plan)
                claim = OldClaim.objects.create(member_id=member.pk, subscription_id=pending.pk,
                                               instruction_id=instruction.pk, plan_name=pending.plan_name,
                                               amount=5000, currency="TZS", payer_phone="+255781234567",
                                               transaction_reference=reference, reference_key=reference)
                return pending, claim
            first, approved = legacy_claim(member1, "UPGRADEAPPROVED1")
            second, pending = legacy_claim(member2, "UPGRADEPENDING2")
            with transaction.atomic():
                entry = ledger.post_entry(manual.POSTING_TYPE,
                    lines=[ledger.debit(ledger.get_platform_account("CASH"), 5000),
                           ledger.credit(ledger.get_platform_account("MEMBERSHIP_DEFERRED_REVENUE"), 5000)],
                    idempotency_key=f"manual-membership:{approved.pk}", source_type=manual.SOURCE_TYPE,
                    source_id=str(approved.pk), external_reference=approved.reference_key, description="Test-only upgrade receipt")
                memberships.activate_with_manual_payment(first, reference_key=approved.reference_key, amount=5000,
                                                           actor=root, submitted_at=approved.created_at, now=timezone.now())
                approved.status = "APPROVED"
                approved.reviewed_by_id = root.pk
                approved.reviewed_at = timezone.now()
                approved.verified_amount = 5000
                approved.verified_reference_key = approved.reference_key
                approved.receipt_confirmed = True
                approved.ledger_entry_id = entry.pk
                approved.save()
            instruction.is_active = False
            instruction.retired_at = timezone.now()
            instruction.retired_by_id = root.pk
            instruction.save()
            baseline = (approved.pk, pending.pk, instruction.pk, entry.pk,
                        JournalEntry.objects.get(pk=entry.pk).external_reference,
                        ledger.get_platform_account("CASH").balance)
            MigrationExecutor(owner).migrate(latest)
            assert guard_findings(owner) == []
            old_approved = ManualMembershipPayment.objects.get(pk=approved.pk)
            old_pending = ManualMembershipPayment.objects.get(pk=pending.pk)
            assert old_approved.quote_id is None and old_pending.quote_id is None
            assert old_approved.status == "APPROVED" and old_pending.status == "PENDING_VERIFICATION"
            assert old_approved.ledger_entry_id == baseline[3]
            assert JournalEntry.objects.get(pk=baseline[3]).external_reference == baseline[4]
            assert ledger.get_platform_account("CASH").balance == baseline[5] == 5000
            legacy_instruction = ManualPaymentInstruction.objects.get(pk=instruction.pk)
            assert legacy_instruction.business_number == "123456" and not legacy_instruction.is_active
            restricted = owner.copy(alias="default")
            restricted.settings_dict.update(USER=settings.DATABASES["runtime"]["USER"],
                                            PASSWORD=settings.DATABASES["runtime"]["PASSWORD"])
            connections["default"] = restricted
            try:
                with restricted.cursor() as cursor:
                    cursor.execute("SELECT CURRENT_USER()")
                    assert cursor.fetchone()[0].split("@")[0] == settings.DATABASES["runtime"]["USER"]
                def approve(claim):
                    return manual.approve(root, claim, verified_amount=5000, verified_reference=claim.reference_key,
                                          receipt_confirmed=True, recipient_confirmed=True,
                                          verified_receiving_number="123456",
                                          verified_recipient_name="TEST ONLY LEGACY")
                assert approve(old_approved)[1] is False
                assert approve(old_pending)[1] is True
                assert approve(old_pending)[1] is False
                assert JournalEntry.objects.count() == 2
                assert ledger.get_platform_account("CASH").balance == 10000
            finally:
                restricted.close()
                connections["default"] = owner
            print("PASS: real pre-0006 Airtel approved and pending records survive 0006/0007 unchanged.")
            print("PASS: original bare ledger receipt and AIRTEL-LIPA activation remain valid.")
            print("PASS: restricted login reviews the legacy pending claim against retired details; duplicate approval posts nothing.")
    finally:
        connections["default"] = owner
        connections.close_all()
        owner.settings_dict["NAME"] = original
        if created:
            assert test_name.startswith("test_") and test_name != original and test_name != "test_clipcash_db"
            with owner.cursor() as cursor:
                cursor.execute(f"DROP DATABASE {owner.ops.quote_name(test_name)}")
            print("Disposable upgrade database removed.")
        owner.close()

if __name__ == "__main__":
    main()
