"""UI-10: present staff-entered payment steps as readable text.

Staff often type light Markdown (``**Lipa number**``, ``1. Dial *150*60#``). Printed with
``linebreaksbr`` the markers showed up literally. This filter escapes the whole text first and
only then recognises a tiny, fixed subset, so no HTML from the stored text can ever reach the page:

* ``**text**`` becomes ``<strong>`` (an unpaired ``**`` is removed, never shown);
* ``*Words*`` becomes ``<em>`` only when the starred text begins with a letter;
* lines starting ``1.`` / ``1)`` become a numbered list, lines starting ``-``, ``*`` or ``•`` a bullet list;
* blank lines separate paragraphs; other line breaks are kept.

Other single asterisks are left alone: USSD codes such as ``*150*60#`` must appear exactly as typed.
"""
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=[^\W\d_])([^*\n]*?[^\s*])\*(?![\w*])")
_NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")


def _inline(text):
    html = _BOLD.sub(r"<strong>\1</strong>", escape(text.strip())).replace("**", "")
    return _EMPHASIS.sub(r"<em>\1</em>", html)


@register.filter(is_safe=True)
def payment_steps(value):
    """Escaped, lightly formatted HTML for payment instructions (see module docstring)."""
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, paragraph, items, kind = [], [], [], None

    def flush_paragraph():
        if paragraph:
            blocks.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def flush_list():
        nonlocal kind
        if items:
            blocks.append(f"<{kind}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{kind}>")
            items.clear()
        kind = None

    for line in lines:
        numbered, bullet = _NUMBERED.match(line), _BULLET.match(line)
        match, tag = (numbered, "ol") if numbered else (bullet, "ul") if bullet else (None, None)
        if match and match.group(1).strip():
            flush_paragraph()
            if kind != tag:
                flush_list()
                kind = tag
            items.append(_inline(match.group(1)))
        elif not line.strip():
            flush_paragraph()
            flush_list()
        else:
            flush_list()
            paragraph.append(_inline(line))
    flush_paragraph()
    flush_list()
    return mark_safe("".join(blocks))
