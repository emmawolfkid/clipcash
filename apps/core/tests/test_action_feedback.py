"""Local action-feedback regression checks. Browser checks opt in with CLIPCASH_FEEDBACK_BROWSER=1.
Uses the guarded test database; Chromium only receives Django-rendered pages and local assets.
"""
import os
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib import messages
from django.contrib.messages.storage.base import Message
from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


class FeedbackTemplateTests(SimpleTestCase):
    def test_successes_expire_unless_persistent_and_text_is_escaped(self):
        html = render_to_string("components/messages.html", {"messages": [
            Message(messages.SUCCESS, "Saved", extra_tags="feedback-auto"),
            Message(messages.SUCCESS, "Another administrator must approve it.", extra_tags="feedback-persist"),
            Message(messages.ERROR, "<script>unsafe</script>", extra_tags="feedback-auto"),
        ]})
        self.assertEqual(html.count(" data-feedback-auto"), 1)
        self.assertEqual(html.count("data-feedback-close hidden"), 3)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", html)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true"', html)
        # Errors are announced urgently, but only through a region the script fills after load:
        # server HTML never puts message text inside role="alert".
        self.assertIn('<div class="visually-hidden" role="alert" aria-atomic="true" data-feedback-urgent></div>', html)
        self.assertEqual(html.count('role="alert"'), 1)


@skipUnless(os.environ.get("CLIPCASH_FEEDBACK_BROWSER") == "1", "Opt-in local Chromium checks")
class FeedbackBrowserTests(TestCase):
    def test_console_feedback(self):
        from playwright.sync_api import sync_playwright
        from apps.accounts.tests.helpers import User, make_user
        from apps.memberships.models import MembershipPlan
        from apps.notifications.models import Notification
        from apps.payments.tests.test_plans_and_renewal import form_data

        self.client.force_login(make_user("feedback-admin@example.com", role=User.Role.SUPER_ADMIN))
        notices_before = Notification.objects.count()
        response = self.client.post(reverse("administration:plan_create"),
                                    form_data(code="tanzanite", name="Tanzanite"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(MembershipPlan.objects.filter(code="tanzanite").exists())
        self.assertContains(response, "Tanzanite plan created")
        self.assertContains(response, "data-feedback-auto")
        self.assertEqual(Notification.objects.count(), notices_before)
        plan_html = response.content.decode()
        invalid = self.client.post(reverse("administration:plan_create"), form_data(code="invalid", price_amount="abc"))
        self.assertEqual(invalid.status_code, 400)
        invalid_html = invalid.content.decode()
        # Use the real shared template, with representative levels and actionable success.
        multi = render_to_string("components/messages.html", {"messages": [
            Message(messages.SUCCESS, "Tanzanite plan created", extra_tags="feedback-auto"),
            Message(messages.SUCCESS, "Policy draft saved. Another administrator must approve it.", extra_tags="feedback-persist"),
            Message(messages.ERROR, "Payment reference does not match. Check the transaction."),
        ]})
        start = plan_html.index('<div class="feedback" data-feedback>')
        end = plan_html.index('</ul>', start) + len('</ul>')
        end = plan_html.index('</div>', end) + len('</div>')
        multi_html = plan_html[:start] + multi + plan_html[end:]
        pages = {"/plan/": plan_html, "/multiple/": multi_html, "/invalid/": invalid_html}
        artifacts = Path(os.environ.get("CLIPCASH_FEEDBACK_ARTIFACTS", settings.BASE_DIR / "logs/action-feedback"))
        artifacts.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe", headless=True)
            def route_local(route):
                from urllib.parse import urlsplit
                path = urlsplit(route.request.url).path
                if path.startswith('/static/'):
                    asset = finders.find(path[len('/static/'):])
                    if asset:
                        route.fulfill(path=asset)
                        return
                if path in pages:
                    route.fulfill(body=pages[path], content_type="text/html")
                else:
                    route.abort()
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            context.route("**/*", route_local)
            page = context.new_page()
            page.set_default_timeout(5000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto("http://testserver/plan/")
            page.wait_for_function("document.querySelector('[data-feedback-announcer]').textContent.includes('Tanzanite')")
            self.assertEqual(page.locator('[data-feedback-announcer]').get_attribute('aria-live'), 'polite')
            self.assertEqual(page.evaluate('document.activeElement.tagName'), 'BODY')
            self.assertIn('status', page.locator('[data-feedback-announcer]').aria_snapshot())
            (artifacts / 'announcement.txt').write_text(page.locator('[data-feedback-announcer]').aria_snapshot(), encoding='utf-8')
            page.screenshot(path=str(artifacts / 'desktop-plan.png'), full_page=True)
            # A real success expires; focus in the current task never changes.
            page.locator('#id_name').focus()
            page.wait_for_timeout(8300)
            self.assertEqual(page.locator('.messages__item').count(), 0)
            self.assertEqual(page.evaluate('document.activeElement.id'), 'id_name')
            page.goto("http://testserver/multiple/")
            self.assertEqual(page.locator('.messages__item').count(), 3)
            close = page.get_by_role('button', name='Dismiss message')
            close.first.focus()
            page.wait_for_timeout(8300)
            self.assertEqual(page.locator('.messages__item').count(), 3, 'Focused success must not expire')
            page.keyboard.press('Enter')
            self.assertEqual(page.locator('.messages__item').count(), 2)
            self.assertEqual(page.evaluate('document.activeElement.hasAttribute("data-feedback-close")'), True)
            page.keyboard.press('Space')
            self.assertEqual(page.locator('.messages__item').count(), 1)
            page.wait_for_timeout(8300)
            self.assertEqual(page.locator('.messages__item').count(), 1, 'Errors persist')
            page.goto("http://testserver/multiple/")
            page.locator('#id_name').focus()
            page.wait_for_timeout(8300)
            self.assertEqual(page.locator('.messages__item').count(), 2, 'Actionable success and error persist')
            page.screenshot(path=str(artifacts / 'desktop-multiple.png'), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto("http://testserver/multiple/")
            page.wait_for_timeout(250)
            page.screenshot(path=str(artifacts / 'mobile-multiple.png'), full_page=True)
            for width in (320, 390, 768):
                page.set_viewport_size({"width": width, "height": 844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                boxes = page.locator('.messages__item').evaluate_all('(cards) => cards.map(c => ({top:c.getBoundingClientRect().top,bottom:c.getBoundingClientRect().bottom,right:c.getBoundingClientRect().right}))')
                for a, b in zip(boxes, boxes[1:]):
                    self.assertLessEqual(a['bottom'], b['top'])
                header = page.locator('.site-header').bounding_box()
                self.assertGreaterEqual(boxes[0]['top'], header['y'] + header['height'])
                button = page.get_by_role('button', name='Save plan').bounding_box()
                self.assertLessEqual(boxes[-1]['bottom'], button['y'])
            page.goto("http://testserver/invalid/")
            self.assertGreater(page.locator('.error-summary').count(), 0)
            self.assertGreater(page.locator('[aria-invalid="true"]').count(), 0)
            self.assertEqual(page.locator('[data-feedback]').count(), 0)
            page.screenshot(path=str(artifacts / 'mobile-validation.png'), full_page=True)
            # No-JS: server text stays readable, dead close controls stay hidden.
            nojs = browser.new_context(java_script_enabled=False, viewport={"width": 390, "height": 844})
            nojs.route("**/*", route_local)
            fallback = nojs.new_page()
            fallback.goto("http://testserver/plan/")
            self.assertTrue(fallback.get_by_text('Tanzanite plan created', exact=True).is_visible())
            self.assertEqual(fallback.get_by_role('button', name='Dismiss message').count(), 0)
            fallback.screenshot(path=str(artifacts / 'mobile-no-javascript.png'), full_page=True)
            self.assertEqual(errors, [])
            (artifacts / 'accessibility.txt').write_text(page.locator('main').aria_snapshot(), encoding='utf-8')
            browser.close()


class PlanFeedbackTests(TestCase):
    def setUp(self):
        from apps.accounts.tests.helpers import User, make_user
        self.client.force_login(make_user("plan-feedback@example.com", role=User.Role.SUPER_ADMIN))

    def test_create_refresh_edit_and_invalid_data_with_payments_disabled(self):
        from apps.memberships.models import MembershipPlan
        from apps.payments.tests.test_plans_and_renewal import form_data
        with self.settings(PAYMENTS_MODE="disabled"):
            before = MembershipPlan.objects.count()
            response = self.client.post(reverse("administration:plan_create"),
                                        form_data(code="feedback", name="Feedback"), follow=True)
            plan = MembershipPlan.objects.get(code="feedback")
            url = reverse("administration:plan", args=[plan.pk])
            self.assertRedirects(response, url)
            self.assertContains(response, "Feedback plan created", count=1)
            self.assertContains(response, "All plans")
            self.assertContains(response, "Payments disabled")
            self.assertNotContains(response, "Can be bought")
            refreshed = self.client.get(url)
            self.assertNotContains(refreshed, "Feedback plan created")
            self.assertEqual(MembershipPlan.objects.count(), before + 1)
            versions = plan.versions.count()
            saved = self.client.post(url, form_data(plan, name="Feedback", description="Updated description"), follow=True)
            self.assertRedirects(saved, url)
            self.assertContains(saved, "Feedback plan saved.", count=1)
            plan.refresh_from_db()
            self.assertEqual(plan.versions.count(), versions)
            self.assertNotContains(self.client.get(url), "Feedback plan saved.")
            invalid = self.client.post(url, form_data(plan, name="Keep my input", price_amount="abc"))
            self.assertEqual(invalid.status_code, 400)
            self.assertContains(invalid, 'value="Keep my input"', status_code=400)
            self.assertContains(invalid, 'aria-invalid="true"', status_code=400)
            from apps.memberships import services as memberships
            from apps.accounts.models import User
            memberships.update_plan(User.objects.get(email="plan-feedback@example.com"), plan, {"price_amount": 13000})
            stale = self.client.post(url, form_data(plan, name="Keep stale input"))
            self.assertEqual(stale.status_code, 400)
            self.assertContains(stale, 'value="Keep stale input"', status_code=400)
            self.assertContains(stale, "Another administrator changed", status_code=400)
            # The re-rendered form acknowledges the current version: a deliberate second save succeeds.
            plan.refresh_from_db()
            self.assertContains(stale, f'name="expected_version" value="{plan.version}"', status_code=400)
            retried = self.client.post(url, form_data(plan, name="Keep stale input"), follow=True)
            self.assertContains(retried, "Keep stale input plan saved.")
