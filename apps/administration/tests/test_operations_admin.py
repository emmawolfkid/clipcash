from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.helpers import AccountsTestCase, User, make_user
from apps.audit.models import AuditEvent
from apps.core.jobs import services as jobs
from apps.core.models import Job, ScheduledTask
from apps.core.tests import job_types  # noqa: F401  (registers test job types)

JOB_LIST = reverse("admin:core_job_changelist")
SCHEDULE_LIST = reverse("admin:core_scheduledtask_changelist")


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(content_type__app_label="core", codename__in=codenames))


class OperationsWorkspaceTests(AccountsTestCase):
    # A plain client: no automatic worker run after admin actions.
    client_class = Client

    def setUp(self):
        super().setUp()
        self.root = make_user("root@example.com", role=User.Role.SUPER_ADMIN)
        self.cm = make_user("cm@example.com", role=User.Role.CONTENT_MANAGER)
        self.failed = jobs.enqueue("tests.permanent", {})
        job = jobs.claim_next("w")
        jobs.run_job(job, "w")

    def action(self, action, job_ids, user):
        self.client.force_login(user)
        return self.client.post(JOB_LIST, {"action": action, "_selected_action": [str(i) for i in job_ids]})

    def test_access_requires_explicit_permission(self):
        self.client.force_login(self.cm)
        for name in ("job", "workerheartbeat", "scheduledtask"):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f"admin:core_{name}_changelist")).status_code, 403)
        grant(self.cm, "view_operations")
        response = self.client.get(JOB_LIST)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "recipient rejected")  # Sanitized summary.
        self.assertIsNone(response.context["action_form"])  # No actions without their permissions.

    def test_retry_requires_retry_permission_and_is_audited(self):
        grant(self.cm, "view_operations")
        self.action("retry_selected", [self.failed.pk], self.cm)
        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, Job.Status.FAILED)

        grant(self.cm, "retry_job")
        self.cm = User.objects.get(pk=self.cm.pk)  # Reset the permission cache.
        self.action("retry_selected", [self.failed.pk], self.cm)
        self.failed.refresh_from_db()
        self.assertEqual(self.failed.status, Job.Status.PENDING)
        event = AuditEvent.objects.get(action="jobs.job.retried")
        self.assertEqual((event.actor_id, event.metadata["previous_status"]), (self.cm.pk, "FAILED"))

    def test_cancel_action_validates_eligibility(self):
        succeeded = jobs.enqueue("tests.noop", {"value": 1})
        jobs.run_job(jobs.claim_next("w"), "w")
        response = self.action("cancel_selected", [self.failed.pk, succeeded.pk], self.root)
        self.assertEqual(response.status_code, 302)
        self.failed.refresh_from_db()
        succeeded.refresh_from_db()
        self.assertEqual((self.failed.status, succeeded.status), (Job.Status.CANCELLED, Job.Status.SUCCEEDED))
        self.assertEqual(AuditEvent.objects.filter(action="jobs.job.cancelled").count(), 1)

    def test_no_creation_editing_or_deletion(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("admin:core_job_add")).status_code, 403)
        change = reverse("admin:core_job_change", args=[self.failed.pk])
        self.assertEqual(self.client.post(change, {"job_type": "os.system"}).status_code, 403)
        delete = reverse("admin:core_job_delete", args=[self.failed.pk])
        self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)

    def test_schedule_pause_is_permissioned_and_audited(self):
        from apps.core.jobs.schedules import enqueue_due_schedules

        enqueue_due_schedules()
        grant(self.cm, "view_operations")
        self.client.force_login(self.cm)
        self.client.post(SCHEDULE_LIST, {"action": "disable_selected", "_selected_action": ["core.health_check"]})
        self.assertTrue(ScheduledTask.objects.get(pk="core.health_check").enabled)

        self.client.force_login(self.root)
        self.client.post(SCHEDULE_LIST, {"action": "disable_selected", "_selected_action": ["core.health_check"]})
        self.assertFalse(ScheduledTask.objects.get(pk="core.health_check").enabled)
        event = AuditEvent.objects.get(action="jobs.schedule.changed")
        self.assertEqual(event.metadata, {"schedule": "core.health_check", "enabled": False})
