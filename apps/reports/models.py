from django.db import models


class ReportAccess(models.Model):
    """No table: holds the report permissions. Reports are read-time aggregates."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("view_members_report", "Can view the members and memberships report"),
            ("view_advertising_report", "Can view the advertisers and campaigns report"),
            ("view_rewards_report", "Can view the simulated rewards report"),
            ("view_payments_report", "Can view the sandbox payments report"),
            ("view_withdrawals_report", "Can view the demo withdrawals report"),
            ("view_operations_report", "Can view the operations report"),
            ("view_audit_report", "Can view the audit and security report"),
            ("view_support_report", "Can view the customer support report"),
            ("export_reports", "Can export reports they may view as CSV"),
        ]
