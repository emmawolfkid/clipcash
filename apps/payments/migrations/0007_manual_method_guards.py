"""Provider-scoped receipts and immutable, issued payment quotes."""
import re
from importlib import import_module
from django.db import migrations
from apps.core.migration_ops import MariaDBRunSQL, PostgreSQLRunSQL, check_error, privilege_error, trigger

old = import_module("apps.payments.migrations.0005_manual_payment_integrity")
membership = import_module("apps.memberships.migrations.0003_lifecycle_integrity")
EXTERNAL_MY = "CASE WHEN BINARY NEW.method = 'AIRTEL_LIPA' THEN NEW.reference_key ELSE CONCAT(NEW.method, ':', NEW.reference_key) END"
EXTERNAL_PG = "CASE WHEN NEW.method = 'AIRTEL_LIPA' THEN NEW.reference_key ELSE NEW.method || ':' || NEW.reference_key END"
QUOTE_MATCH = """
    SELECT 1 FROM payments_manualpaymentquote q WHERE q.id = NEW.quote_id
       AND q.subscription_id = NEW.subscription_id AND q.instruction_id = NEW.instruction_id
       AND q.amount = NEW.amount AND q.currency = NEW.currency
"""
INSTRUCTION_MATCH = """
    SELECT 1 FROM payments_manualpaymentinstruction i
     WHERE i.id = NEW.instruction_id AND BINARY i.method = BINARY NEW.method
"""
QUOTE_INSERT = """
    SELECT 1 FROM memberships_subscription s JOIN payments_manualpaymentinstruction i ON i.id = NEW.instruction_id
     WHERE s.id = NEW.subscription_id AND BINARY s.status = 'PENDING' AND i.is_active
       AND s.pending_until > UTC_TIMESTAMP(6) AND s.price_amount = NEW.amount AND s.currency = NEW.currency
"""
maria = []
for statement in old.MARIADB_FORWARD:
    if "CREATE TRIGGER payments_manual_instruction_insert " in statement:
        statement = statement[:-3] + f"""
    IF BINARY NEW.method NOT REGEXP BINARY '^[A-Z][A-Z0-9_]{{1,19}}$' OR CHAR_LENGTH(TRIM(NEW.provider_label)) < 2 THEN
        {check_error("invalid provider code or label")}
    END IF;
END"""
    elif "CREATE TRIGGER payments_manual_instruction_update " in statement:
        statement = statement.replace("BEGIN", "BEGIN\n    IF NOT (BINARY NEW.provider_label <=> BINARY OLD.provider_label) THEN\n"
                                      + privilege_error("issued provider labels are immutable") + "\n    END IF;", 1)
    elif "CREATE TRIGGER payments_manual_claim_insert " in statement:
        statement = statement[:-3] + f"""
    IF NOT EXISTS ({INSTRUCTION_MATCH}) OR
       (NEW.quote_id IS NULL AND BINARY NEW.method <> 'AIRTEL_LIPA') OR
       (NEW.quote_id IS NOT NULL AND NOT EXISTS ({QUOTE_MATCH})) THEN
        {check_error("claim must match its issued provider instructions and quote")}
    END IF;
END"""
    elif "CREATE TRIGGER payments_manual_claim_update " in statement:
        # Keep DECLARE first in the block.
        statement = statement.replace("    IF BINARY OLD.status", "    IF NOT (NEW.quote_id <=> OLD.quote_id) THEN\n"
                                      + privilege_error("issued quote cannot change") + "\n    END IF;\n    IF BINARY OLD.status", 1)
        statement = statement.replace("BINARY NEW.reference_key AND e.total_amount", f"BINARY ({EXTERNAL_MY}) AND e.total_amount")
    else:
        continue
    name = re.search(r"CREATE TRIGGER (\w+)", statement).group(1)
    maria += [f"DROP TRIGGER {name}", statement]
maria += [
    trigger("payments_manual_quote_insert", "BEFORE INSERT", "payments_manualpaymentquote", f"""
    IF NOT EXISTS ({QUOTE_INSERT}) THEN
        {check_error("quote must use active instructions and the pending subscription price")}
    END IF;"""),
    trigger("payments_manual_quote_update", "BEFORE UPDATE", "payments_manualpaymentquote",
            privilege_error("issued payment quotes are immutable")),
    trigger("payments_manual_quote_delete", "BEFORE DELETE", "payments_manualpaymentquote",
            privilege_error("issued payment quotes cannot be deleted")),
]
ACTIVATION_MY = """
    IF BINARY OLD.status = 'PENDING' AND BINARY NEW.status = 'ACTIVE'
       AND (NEW.payment_reference LIKE BINARY 'AIRTEL-LIPA:%' OR NEW.payment_reference LIKE BINARY 'MANUAL:%')
       AND NOT EXISTS (
        SELECT 1 FROM wallets_journalentry e JOIN payments_manualmembershippayment c
          ON BINARY e.source_id = BINARY CAST(c.id AS CHAR)
         WHERE e.status = 'POSTED' AND e.posting_type = 'membership.manual_payment_received'
           AND e.source_type = 'manual_membership_payment' AND c.subscription_id = NEW.id
           AND c.member_id = NEW.member_id AND e.total_amount = NEW.amount_paid AND c.amount = NEW.amount_paid
           AND BINARY e.external_reference = BINARY CASE WHEN c.method = 'AIRTEL_LIPA'
               THEN c.reference_key ELSE CONCAT(c.method, ':', c.reference_key) END
           AND BINARY NEW.payment_reference = BINARY CASE WHEN c.method = 'AIRTEL_LIPA'
               THEN CONCAT('AIRTEL-LIPA:', c.reference_key) ELSE CONCAT('MANUAL:', c.method, ':', c.reference_key) END
       ) THEN
        SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025,
            MESSAGE_TEXT = 'manual activation needs its own finalized provider receipt';
    END IF;
"""
for statement in membership.MARIADB_FORWARD:
    if "CREATE TRIGGER membership_subscription_update " in statement:
        start = statement.index("    -- DB-01 ledger phase:")
        end = statement.index("    IF BINARY NEW.status IN ('ACTIVE', 'SUSPENDED')", start)
        maria += ["DROP TRIGGER membership_subscription_update", statement[:start] + ACTIVATION_MY + statement[end:]]

# Reuse the existing pinned functions; introduce no new unpinned function.
instruction_pg, claim_pg = re.findall(r"CREATE OR REPLACE FUNCTION.*?\$\$;", old.FORWARD, re.S)
instruction_pg = instruction_pg.replace("BEGIN", """BEGIN
    IF TG_TABLE_NAME = 'payments_manualpaymentquote' THEN
        IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'issued payment quotes are immutable' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT EXISTS (""" + QUOTE_INSERT.replace("BINARY ", "").replace("UTC_TIMESTAMP(6)", "CURRENT_TIMESTAMP") + """) THEN
            RAISE EXCEPTION 'quote must use active instructions and the pending subscription price'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
""", 1)
instruction_pg = instruction_pg.replace("IF NEW.method <> OLD.method", "IF NEW.provider_label IS DISTINCT FROM OLD.provider_label OR NEW.method <> OLD.method")
claim_pg = claim_pg.replace("        RETURN NEW;", """
        IF NOT EXISTS (""" + INSTRUCTION_MATCH.replace("BINARY ", "") + """)
           OR (NEW.quote_id IS NULL AND NEW.method <> 'AIRTEL_LIPA')
           OR (NEW.quote_id IS NOT NULL AND NOT EXISTS (""" + QUOTE_MATCH + """)) THEN
            RAISE EXCEPTION 'claim must match issued provider instructions and quote' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;""", 1)
claim_pg = claim_pg.replace("IF NEW.reference <> OLD.reference", "IF NEW.quote_id IS DISTINCT FROM OLD.quote_id OR NEW.reference <> OLD.reference")
claim_pg = claim_pg.replace("    RETURN NEW;\nEND;", """
    IF NEW.status = 'APPROVED' AND NOT EXISTS (
        SELECT 1 FROM wallets_journalentry e WHERE e.id = NEW.ledger_entry_id AND e.status = 'POSTED'
           AND e.posting_type = 'membership.manual_payment_received' AND e.source_type = 'manual_membership_payment'
           AND e.source_id = NEW.id::text AND e.external_reference = (""" + EXTERNAL_PG + """)
           AND e.total_amount = NEW.amount) THEN
        RAISE EXCEPTION 'approval needs its finalized provider receipt' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;""")
subscription_pg = re.findall(r"CREATE OR REPLACE FUNCTION.*?\$\$;", membership.FORWARD, re.S)[0]
activation_pg = ACTIVATION_MY.replace("BINARY ", "").replace("CAST(c.id AS CHAR)", "c.id::text")
activation_pg = activation_pg.replace("""SIGNAL SQLSTATE '23000' SET MYSQL_ERRNO = 4025,
            MESSAGE_TEXT = 'manual activation needs its own finalized provider receipt';""",
    "RAISE EXCEPTION 'manual activation needs its finalized provider receipt' USING ERRCODE = 'check_violation';")
subscription_pg = subscription_pg.replace("LANGUAGE plpgsql AS", "LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS")
subscription_pg = subscription_pg.replace("    RETURN NEW;\nEND;", activation_pg + "    RETURN NEW;\nEND;")
postgres = instruction_pg + claim_pg + subscription_pg + """
CREATE TRIGGER payments_manual_quote_guard BEFORE INSERT OR UPDATE OR DELETE ON payments_manualpaymentquote
FOR EACH ROW EXECUTE FUNCTION payments_manual_instruction_guard();
"""

class Migration(migrations.Migration):
    dependencies = [("payments", "0006_configurable_manual_methods"), ("core", "0004_pin_function_search_path")]
    operations = [PostgreSQLRunSQL(postgres, reverse_sql=None), MariaDBRunSQL(maria, reverse_sql=None)]
