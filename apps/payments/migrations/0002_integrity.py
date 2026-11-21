"""
Database-enforced payment integrity (PostgreSQL), SANDBOX FIRST.

* Nothing here is ever deleted.
* Payment intents: created CREATED with no evidence; purpose, funds domain,
  payer, advertiser, membership, amount, currency, reference, environment,
  provider and idempotency key never change; the provider reference is written
  once; status follows
      CREATED  -> PENDING | CANCELLED | EXPIRED | FAILED
      PENDING  -> PROVIDER_CONFIRMED | FAILED | EXPIRED | CANCELLED
      PROVIDER_CONFIRMED -> RECONCILED | FAILED (rejected after review)
      RECONCILED -> SETTLED;  SETTLED -> DISPUTED | REFUNDED
      DISPUTED -> SETTLED (won) | REFUNDED (lost); closed states are terminal.
* Provider events and sandbox journal postings are append-only.
* Allocations: only to the advertiser's own campaign; amount fixed;
  ACTIVE -> RELEASED only.
* Refunds and disputes are decided once; exceptions are resolved once.

Limitation: the table owner or a superuser can disable these triggers.
"""

from django.db import migrations

from apps.core.migration_ops import (
    MariaDBRunSQL,
    PostgreSQLRunSQL,
    changed,
    check_error,
    drop_triggers,
    privilege_error,
    trigger,
)

FORWARD = r"""
CREATE OR REPLACE FUNCTION payments_no_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be deleted', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION payments_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE OR REPLACE FUNCTION payments_intent_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'CREATED' OR NEW.confirmed_at IS NOT NULL OR NEW.reconciled_at IS NOT NULL
           OR NEW.settled_at IS NOT NULL OR NEW.provider_reference <> '' THEN
            RAISE EXCEPTION 'payment intents must be created empty' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.reference <> OLD.reference OR NEW.environment <> OLD.environment OR NEW.purpose <> OLD.purpose
       OR NEW.funds_domain <> OLD.funds_domain OR NEW.payer_id <> OLD.payer_id
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.subscription_id IS DISTINCT FROM OLD.subscription_id OR NEW.amount <> OLD.amount
       OR NEW.currency <> OLD.currency OR NEW.provider <> OLD.provider
       OR NEW.idempotency_key <> OLD.idempotency_key OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'payment intent terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.provider_reference <> '' AND NEW.provider_reference <> OLD.provider_reference THEN
        RAISE EXCEPTION 'a provider reference cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.provider_amount IS NOT NULL AND (NEW.provider_amount IS DISTINCT FROM OLD.provider_amount
       OR NEW.provider_currency <> OLD.provider_currency) THEN
        RAISE EXCEPTION 'provider evidence cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'CREATED' AND NEW.status IN ('PENDING', 'CANCELLED', 'EXPIRED', 'FAILED'))
        OR (OLD.status = 'PENDING' AND NEW.status IN ('PROVIDER_CONFIRMED', 'FAILED', 'EXPIRED', 'CANCELLED'))
        OR (OLD.status = 'PROVIDER_CONFIRMED' AND NEW.status IN ('RECONCILED', 'FAILED'))
        OR (OLD.status = 'RECONCILED' AND NEW.status = 'SETTLED')
        OR (OLD.status = 'SETTLED' AND NEW.status IN ('DISPUTED', 'REFUNDED'))
        OR (OLD.status = 'DISPUTED' AND NEW.status IN ('SETTLED', 'REFUNDED'))
    ) THEN
        RAISE EXCEPTION 'invalid payment status change % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payments_allocation_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner uuid;
BEGIN
    SELECT organization_id INTO owner FROM advertising_campaign WHERE id = NEW.campaign_id;
    IF owner IS DISTINCT FROM NEW.organization_id THEN
        RAISE EXCEPTION 'funds can only be allocated to the advertiser''s own campaign'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'ACTIVE' THEN
            RAISE EXCEPTION 'allocations are created active' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.amount <> OLD.amount OR NEW.campaign_id <> OLD.campaign_id
       OR NEW.organization_id <> OLD.organization_id THEN
        RAISE EXCEPTION 'allocation terms cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.status <> OLD.status AND NOT (OLD.status = 'ACTIVE' AND NEW.status = 'RELEASED') THEN
        RAISE EXCEPTION 'invalid allocation change % -> %', OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION payments_decided_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status NOT IN ('REQUESTED', 'OPEN') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION '% decisions are final', TG_TABLE_NAME USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER payments_intent_guard BEFORE INSERT OR UPDATE ON payments_paymentintent
    FOR EACH ROW EXECUTE FUNCTION payments_intent_guard();
CREATE TRIGGER payments_allocation_guard BEFORE INSERT OR UPDATE ON payments_sandboxcampaignallocation
    FOR EACH ROW EXECUTE FUNCTION payments_allocation_guard();
CREATE TRIGGER payments_refund_decided BEFORE UPDATE ON payments_refundrequest
    FOR EACH ROW EXECUTE FUNCTION payments_decided_once();
CREATE TRIGGER payments_dispute_decided BEFORE UPDATE ON payments_paymentdispute
    FOR EACH ROW EXECUTE FUNCTION payments_decided_once();
CREATE TRIGGER payments_exception_decided BEFORE UPDATE ON payments_reconciliationexception
    FOR EACH ROW EXECUTE FUNCTION payments_decided_once();
CREATE TRIGGER payments_posting_append_only BEFORE UPDATE OR DELETE ON payments_sandboxposting
    FOR EACH ROW EXECUTE FUNCTION payments_append_only();
CREATE TRIGGER payments_event_append_only BEFORE DELETE ON payments_providerevent
    FOR EACH ROW EXECUTE FUNCTION payments_append_only();
CREATE TRIGGER payments_preview_append_only BEFORE UPDATE OR DELETE ON payments_membershipactivationpreview
    FOR EACH ROW EXECUTE FUNCTION payments_append_only();
"""

TABLES = ["payments_paymentintent", "payments_sandboxadvertiserfunds", "payments_sandboxcampaignallocation",
          "payments_refundrequest", "payments_paymentdispute", "payments_reconciliationexception",
          "payments_sandboxprovidertransaction"]
FORWARD += "".join(f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW "
                   f"EXECUTE FUNCTION payments_no_delete();\n" for t in TABLES)

REVERSE = "".join(f"DROP TRIGGER IF EXISTS {t}_no_delete ON {t};\n" for t in TABLES) + r"""
DROP TRIGGER IF EXISTS payments_preview_append_only ON payments_membershipactivationpreview;
DROP TRIGGER IF EXISTS payments_event_append_only ON payments_providerevent;
DROP TRIGGER IF EXISTS payments_posting_append_only ON payments_sandboxposting;
DROP TRIGGER IF EXISTS payments_exception_decided ON payments_reconciliationexception;
DROP TRIGGER IF EXISTS payments_dispute_decided ON payments_paymentdispute;
DROP TRIGGER IF EXISTS payments_refund_decided ON payments_refundrequest;
DROP TRIGGER IF EXISTS payments_allocation_guard ON payments_sandboxcampaignallocation;
DROP TRIGGER IF EXISTS payments_intent_guard ON payments_paymentintent;
DROP FUNCTION IF EXISTS payments_decided_once();
DROP FUNCTION IF EXISTS payments_allocation_guard();
DROP FUNCTION IF EXISTS payments_intent_guard();
DROP FUNCTION IF EXISTS payments_append_only();
DROP FUNCTION IF EXISTS payments_no_delete();
"""


# DB-01: the same rules on MariaDB (one trigger per event).
INTENT_TERMS = ("reference", "environment", "purpose", "funds_domain", "payer_id", "organization_id",
                "subscription_id", "amount", "currency", "provider", "idempotency_key", "created_at")
ALLOCATION_OWNER = f"""
    DECLARE owner UUID;
    SELECT organization_id INTO owner FROM advertising_campaign WHERE id = NEW.campaign_id;
    IF NOT (owner <=> NEW.organization_id) THEN
        {privilege_error("funds can only be allocated to the advertiser's own campaign")}
    END IF;"""
DECIDED_ONCE = """
    IF BINARY OLD.status NOT IN ('REQUESTED', 'OPEN') AND NOT (BINARY NEW.status <=> BINARY OLD.status) THEN
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = '{table} decisions are final';
    END IF;"""
APPEND_ONLY = {  # (trigger name, table): events refused
    ("payments_posting", "payments_sandboxposting"): ("UPDATE", "DELETE"),
    ("payments_event", "payments_providerevent"): ("DELETE",),
    ("payments_preview", "payments_membershipactivationpreview"): ("UPDATE", "DELETE"),
}
NO_DELETE = ("payments_paymentintent", "payments_sandboxadvertiserfunds", "payments_sandboxcampaignallocation",
             "payments_refundrequest", "payments_paymentdispute", "payments_reconciliationexception",
             "payments_sandboxprovidertransaction")

MARIADB_FORWARD = [
    trigger("payments_intent_insert", "BEFORE INSERT", "payments_paymentintent", f"""
    IF NOT (BINARY NEW.status <=> 'CREATED') OR NEW.confirmed_at IS NOT NULL OR NEW.reconciled_at IS NOT NULL
       OR NEW.settled_at IS NOT NULL OR NEW.provider_reference <> '' THEN
        {check_error("payment intents must be created empty")}
    END IF;"""),
    trigger("payments_intent_update", "BEFORE UPDATE", "payments_paymentintent", f"""
    DECLARE message VARCHAR(200);
    IF {changed(*INTENT_TERMS)} THEN
        {privilege_error("payment intent terms cannot change")}
    END IF;
    IF OLD.provider_reference <> '' AND {changed("provider_reference")} THEN
        {privilege_error("a provider reference cannot change")}
    END IF;
    IF OLD.provider_amount IS NOT NULL AND {changed("provider_amount", "provider_currency")} THEN
        {privilege_error("provider evidence cannot change")}
    END IF;
    IF {changed("status")} AND NOT (
        (BINARY OLD.status = 'CREATED' AND BINARY NEW.status IN ('PENDING', 'CANCELLED', 'EXPIRED', 'FAILED'))
        OR (BINARY OLD.status = 'PENDING' AND BINARY NEW.status IN ('PROVIDER_CONFIRMED', 'FAILED', 'EXPIRED', 'CANCELLED'))
        OR (BINARY OLD.status = 'PROVIDER_CONFIRMED' AND BINARY NEW.status IN ('RECONCILED', 'FAILED'))
        OR (BINARY OLD.status = 'RECONCILED' AND BINARY NEW.status = 'SETTLED')
        OR (BINARY OLD.status = 'SETTLED' AND BINARY NEW.status IN ('DISPUTED', 'REFUNDED'))
        OR (BINARY OLD.status = 'DISPUTED' AND BINARY NEW.status IN ('SETTLED', 'REFUNDED'))
    ) THEN
        SET message = CONCAT('invalid payment status change ', OLD.status, ' -> ', NEW.status);
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025, MESSAGE_TEXT = message;
    END IF;"""),
    trigger("payments_allocation_insert", "BEFORE INSERT", "payments_sandboxcampaignallocation", ALLOCATION_OWNER + f"""
    IF NOT (BINARY NEW.status <=> 'ACTIVE') THEN
        {check_error("allocations are created active")}
    END IF;"""),
    trigger("payments_allocation_update", "BEFORE UPDATE", "payments_sandboxcampaignallocation", ALLOCATION_OWNER + f"""
    IF {changed("amount", "campaign_id", "organization_id")} THEN
        {privilege_error("allocation terms cannot change")}
    END IF;
    IF {changed("status")} AND NOT (BINARY OLD.status = 'ACTIVE' AND BINARY NEW.status = 'RELEASED') THEN
        {check_error("invalid allocation status change")}
    END IF;"""),
] + [
    trigger(f"{name}_decided", "BEFORE UPDATE", table, DECIDED_ONCE.format(table=table))
    for name, table in (("payments_refund", "payments_refundrequest"), ("payments_dispute", "payments_paymentdispute"),
                        ("payments_exception", "payments_reconciliationexception"))
] + [
    trigger(f"{name}_{event.lower()}", f"BEFORE {event}", table, privilege_error(f"{table} rows are append-only"))
    for (name, table), events in APPEND_ONLY.items() for event in events
] + [
    trigger(f"{table}_no_delete", "BEFORE DELETE", table, privilege_error(f"{table} rows cannot be deleted"))
    for table in NO_DELETE
]

MARIADB_REVERSE = drop_triggers(*reversed([
    "payments_intent_insert", "payments_intent_update", "payments_allocation_insert", "payments_allocation_update",
    "payments_refund_decided", "payments_dispute_decided", "payments_exception_decided",
    *[f"{name}_{event.lower()}" for (name, _table), events in APPEND_ONLY.items() for event in events],
    *[f"{table}_no_delete" for table in NO_DELETE],
]))


class Migration(migrations.Migration):
    dependencies = [("payments", "0001_initial"), ("advertising", "0002_integrity")]

    operations = [
        PostgreSQLRunSQL(FORWARD, REVERSE),
        MariaDBRunSQL(MARIADB_FORWARD, MARIADB_REVERSE),
    ]
