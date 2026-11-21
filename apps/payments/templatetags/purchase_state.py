"""PAY-LIVE-01C: the one "purchases closed" explanation, for templates.

Flash messages and refusals use apps.payments.manual.PURCHASE_CLOSED_MESSAGE directly; this tag
gives templates the same text, so members never see two different explanations.
"""

from django import template

from apps.payments.manual import PURCHASE_CLOSED_MESSAGE

register = template.Library()


@register.simple_tag
def purchase_closed_message():
    return PURCHASE_CLOSED_MESSAGE
