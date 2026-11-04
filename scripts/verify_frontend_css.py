"""Dependency-free structural checks for the project's external CSS modules.

Checks rule/declaration duplication and template references, not browser layout.
--baseline can compare effective declarations against a pre-extraction snapshot.
"""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def split_top(text, delimiter=","):
    start = depth = 0
    quote = None
    escaped = False
    result = []
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == delimiter and depth == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return [part for part in result if part]


def rules(text, conditions=()):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    position = 0
    while position < len(text):
        opening = text.find("{", position)
        if opening < 0:
            assert not text[position:].strip(), "Unexpected trailing CSS"
            break
        selector = " ".join(text[position:opening].split())
        depth, end, quote = 1, opening + 1, None
        while depth:
            char = text[end]
            if quote:
                if char == quote and text[end - 1] != "\\":
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            end += 1
        body = text[opening + 1:end - 1]
        if selector.startswith("@"):
            yield from rules(body, conditions + (selector,))
        else:
            declarations = [tuple(part.strip() for part in declaration.split(":", 1))
                            for declaration in split_top(body, ";")]
            for item in split_top(selector):
                yield (conditions, item), declarations
        position = end


def verify(baseline=None):
    paths = sorted((ROOT / "static/css").rglob("*.css"))
    actual = {}
    for path in paths:
        parsed = list(rules(path.read_text(encoding="utf-8")))
        assert parsed, f"Empty stylesheet: {path}"
        for key, declarations in parsed:
            assert key not in actual, f"Duplicate selector/condition: {key}"
            assert len(dict(declarations)) == len(declarations), f"Repeated property: {key}"
            actual[key] = dict(declarations)
    templates = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "templates").rglob("*.html"))
    references = set(re.findall(r"{% static ['\"](css/[^'\"]+\.css)['\"] %}", templates))
    expected_paths = {path.relative_to(ROOT / "static").as_posix() for path in paths}
    assert references == expected_paths, {"missing": references - expected_paths, "unreferenced": expected_paths - references}
    if baseline:
        expected = {}
        for name in ("tokens.css", "clipcash.css", "design-system.css"):
            for key, declarations in rules((baseline / name).read_text(encoding="utf-8")):
                expected.setdefault(key, {}).update(declarations)
        assert actual == expected, "Effective selector declarations changed during extraction"
    print(f"PASS: {len(paths)} nonempty modules; {len(actual)} unique selector/condition pairs; "
          "no repeated properties, missing references or unreferenced modules.")
    if baseline:
        print("PASS: all effective selector declarations match the original three-file snapshot.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    verify(parser.parse_args().baseline)
