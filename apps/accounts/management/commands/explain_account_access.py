"""PRD-03B: explain, read-only, where an account lands after signing in and why.

    python manage.py explain_account_access <email, mobile number or username>

Prints the account's role and status, which identifiers it has (not their values), whether each
is confirmed, and the decisions the application makes for it: page access, the staff confirmation
rule, sign-in by email, password reset, and buying a membership. Changes nothing and sends nothing.
Use it on the server when someone reports being held at /accounts/verify/required/.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.access import account_ready, has_verified_contact, identity_ready
from apps.accounts.forms import _account_for, sign_in_key


class Command(BaseCommand):
    help = "Explain (read-only) why an account can or cannot open member pages, sign in and recover."

    def add_arguments(self, parser):
        parser.add_argument("identifier", help="The email address, mobile number or username the person signs in with.")

    def handle(self, *args, identifier, **options):
        account = _account_for(sign_in_key(identifier))
        if account is None:
            raise CommandError("No account uses that identifier.")
        yes = lambda value: "yes" if value else "no"  # noqa: E731
        lines = [
            f"role: {account.role}   status: {account.status}",
            f"identifiers: email={yes(account.email)} (confirmed {yes(account.email_verified)}), "
            f"mobile={yes(account.phone_number)} (verified {yes(account.phone_verified)}), "
            f"username={yes(account.username)}",
            f"created by public sign-up (email_ownership_required): {yes(account.email_ownership_required)}",
            f"can sign in: {yes(account.status == account.Status.ACTIVE)}"
            f"   by email: {yes(account.status == account.Status.ACTIVE and account.email_signs_in)}",
            f"opens member pages: {yes(account.status == account.Status.ACTIVE and account_ready(account))}",
            f"password reset by email possible: {yes(account.email_recovers_account)}",
            f"may start a paid membership (confirmed contact): {yes(has_verified_contact(account))}",
            f"financial/identity eligibility (identity_ready): {yes(identity_ready(account))}",
        ]
        if account.status == account.Status.ACTIVE and not account_ready(account):
            lines.append("HELD at /accounts/verify/required/: staff accounts need a confirmed email address or a "
                         "mobile number. Use 'Send a new link' on that page (SMTP and the run_jobs cron must work).")
        elif account.status != account.Status.ACTIVE:
            lines.append("Cannot sign in: the account is not ACTIVE.")
        else:
            lines.append("Not held: signing in leads to the dashboard. If someone still sees the confirmation page, "
                         "check that the server runs this release (Passenger restarted: touch tmp/restart.txt).")
        for line in lines:
            self.stdout.write(line)
