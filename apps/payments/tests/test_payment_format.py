"""UI-10: staff-entered payment steps render as safe, readable text."""
from django.template import Context, Template
from django.test import SimpleTestCase

from apps.payments.templatetags.payment_format import payment_steps


class PaymentStepsTests(SimpleTestCase):
    def test_bold_markers_become_strong_and_never_show(self):
        html = payment_steps("Pay to the **Lipa number** above. Unpaired ** marker.")
        self.assertEqual(html, "<p>Pay to the <strong>Lipa number</strong> above. Unpaired  marker.</p>")
        self.assertNotIn("**", html)

    def test_numbered_and_bullet_lines_become_lists(self):
        html = payment_steps("Steps:\n1. Open Airtel Money\n2) Choose Lipa kwa Simu\n\n- Keep the SMS\n• Note the ID")
        self.assertEqual(html, "<p>Steps:</p><ol><li>Open Airtel Money</li><li>Choose Lipa kwa Simu</li></ol>"
                               "<ul><li>Keep the SMS</li><li>Note the ID</li></ul>")

    def test_ussd_codes_and_single_asterisks_are_kept_exactly(self):
        self.assertEqual(payment_steps("Dial *150*60#."), "<p>Dial *150*60#.</p>")
        self.assertEqual(payment_steps("1. Dial *150*60#"), "<ol><li>Dial *150*60#</li></ol>")
        self.assertEqual(payment_steps("Dial *150*60# then *123#"), "<p>Dial *150*60# then *123#</p>")
        self.assertEqual(payment_steps("2 * 3 = 6"), "<p>2 * 3 = 6</p>")

    def test_starred_words_become_emphasis(self):
        self.assertEqual(payment_steps("Choose *Lipa kwa Simu*, then **Pay**."),
                         "<p>Choose <em>Lipa kwa Simu</em>, then <strong>Pay</strong>.</p>")

    def test_markup_is_always_escaped(self):
        html = payment_steps('<script>alert(1)</script>\n**<img src=x onerror=alert(1)>**\n- <b>x</b>')
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<strong>&lt;img src=x onerror=alert(1)&gt;</strong>", html)

    def test_paragraphs_line_breaks_and_empty_values(self):
        self.assertEqual(payment_steps("One\nTwo\n\nThree"), "<p>One<br>Two</p><p>Three</p>")
        self.assertEqual(payment_steps("\r\n"), "")
        self.assertEqual(payment_steps(None), "")

    def test_filter_output_is_not_escaped_twice_in_templates(self):
        rendered = Template("{% load payment_format %}{{ text|payment_steps }}").render(
            Context({"text": "**A** & B"}))
        self.assertEqual(rendered, "<p><strong>A</strong> &amp; B</p>")
