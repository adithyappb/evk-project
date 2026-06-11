"""Inject CSRF hidden fields into HTML form templates."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "evk" / "ui" / "templates"


def main() -> None:
    for path in ROOT.rglob("*.html"):
        if path.name == "_csrf_input.html":
            continue
        text = path.read_text(encoding="utf-8")
        if 'method="post"' not in text.lower():
            continue
        if "csrf_input" in text:
            continue
        import_line = '{% from "_csrf_input.html" import csrf_input %}'
        if import_line not in text:
            if "{% extends" in text:
                text = text.replace("{% extends", f"{import_line}\n{{% extends", 1)
            else:
                text = f"{import_line}\n{text}"
        text = re.sub(
            r'(<form[^>]*method="post"[^>]*>)',
            r'\1\n        {{ csrf_input() }}',
            text,
            flags=re.IGNORECASE,
        )
        path.write_text(text, encoding="utf-8")
        print(f"updated {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
