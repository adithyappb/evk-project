from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src" / "evk" / "ui" / "templates"
for path in root.rglob("*.html"):
    text = path.read_text(encoding="utf-8")
    updated = text.replace('{% from "_csrf_input.html" import csrf_input %}\n', "")
    updated = updated.replace("{{ csrf_input() }}", '{% include "_csrf_input.html" %}')
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        print("fixed", path.name)
