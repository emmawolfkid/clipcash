"""Validate/compile standard Django PO catalogues without requiring GNU gettext on Windows.

Run with --compile after editing django.po. --inventory lists untranslated project
strings using Python's AST and Django's own template extraction, excluding migrations.
Runtime translation remains entirely Django/gettext; no application hook is installed.
"""
import argparse
import ast
import gettext
import json
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
PO = ROOT / "locale/sw/LC_MESSAGES/django.po"
MO = PO.with_suffix(".mo")


def read_po():
    entries, entry, field = [], {}, None
    for line in PO.read_text(encoding="utf-8").splitlines() + [""]:
        if not line.strip():
            if entry:
                entries.append(entry)
            entry, field = {}, None
        elif line.startswith("#"):
            if "fuzzy" in line:
                raise ValueError("Fuzzy translations must be reviewed before compilation")
        elif line.startswith('"'):
            entry[field] += ast.literal_eval(line)
        else:
            field, value = line.split(" ", 1)
            entry[field] = ast.literal_eval(value)
    return entries


def catalogue():
    messages = {}
    for entry in read_po():
        source = entry["msgid"]
        plural = entry.get("msgid_plural")
        key = source if plural is None else source + "\0" + plural
        value = entry.get("msgstr", "") if plural is None else "\0".join(entry[f"msgstr[{i}]"] for i in range(2))
        if key in messages:
            raise ValueError(f"Duplicate msgid: {source}")
        if source and not value:
            raise ValueError(f"Empty translation: {source}")
        if source:
            required = set(re.findall(r"%\([^)]+\)[sdi]", source))
            for translated in value.split("\0"):
                if set(re.findall(r"%\([^)]+\)[sdi]", translated)) != required:
                    raise ValueError(f"Changed interpolation placeholders: {source}")
        messages[key] = value
    return messages


def compile_mo(messages):
    keys = sorted(messages)
    originals = [key.encode("utf-8") for key in keys]
    translations = [messages[key].encode("utf-8") for key in keys]
    offset = 28 + 16 * len(keys)
    original_table, translated_table = [], []
    for values, table in ((originals, original_table), (translations, translated_table)):
        for value in values:
            table.extend((len(value), offset))
            offset += len(value) + 1
    header = struct.pack("<7I", 0x950412DE, 0, len(keys), 28, 28 + 8 * len(keys), 0, 0)
    tables = struct.pack(f"<{4 * len(keys)}I", *(original_table + translated_table))
    return header + tables + b"".join(value + b"\0" for value in originals + translations)


def inventory():
    from django.utils.translation.template import templatize
    result = {}
    names = {"_", "gettext", "gettext_lazy", "gettext_noop", "ngettext", "ngettext_lazy", "pgettext", "pgettext_lazy"}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if any(part in {"tests", "migrations", "management"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names:
                args = node.args[1:] if node.func.id.startswith("pgettext") else node.args
                if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    result.setdefault(args[0].value, set()).add(path.relative_to(ROOT).as_posix())
    literal = r"(?:u)?(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    for path in sorted((ROOT / "templates").rglob("*.html")):
        extracted = templatize(path.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?:gettext|ngettext)\((" + literal + r")", extracted):
            result.setdefault(ast.literal_eval(match[1]), set()).add(path.relative_to(ROOT).as_posix())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile", action="store_true")
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args()
    messages = catalogue() if PO.exists() else {}
    if args.compile:
        MO.write_bytes(compile_mo(messages))
    if messages:
        with MO.open("rb") as stream:
            compiled = gettext.GNUTranslations(stream)
        assert compiled._catalog[""] == messages[""], "Stale catalogue header"
        assert MO.read_bytes() == compile_mo(messages), "Stale MO: run --compile"
        print(f"Validated {len(messages) - 1} Swahili entries; PO and MO match.")
    if args.inventory:
        missing = {key: sorted(paths) for key, paths in inventory().items() if key not in messages and not any(k.startswith(key + '\0') for k in messages)}
        output = ROOT / "logs/ui09_translation_gaps.json"
        output.write_text(json.dumps(missing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(missing)} untranslated project messages. Inventory: {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
