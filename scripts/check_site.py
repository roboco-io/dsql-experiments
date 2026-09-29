"""Check the rendered Pages site without extra Python dependencies."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.ids = set()
        self.language = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "html":
            self.language = attrs.get("lang")
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if name in attrs:
                self.links.append(attrs[name])


root = Path(__file__).resolve().parent.parent
site = root / "_site"
files = sorted(site.rglob("*.html"))
if not files or not (site / "index.html").exists():
    sys.exit("Build the site before checking it")
pages = {}
errors = []
for path in files:
    page = Page()
    content = path.read_text()
    page.feed(content)
    pages[path.resolve()] = page
    if page.language != "ko":
        errors.append(f"{path}: missing Korean language metadata")
    if "{{" in content or "{%" in content:
        errors.append(f"{path}: unresolved Liquid template")

base = "/dsql-experiments"
for path, page in pages.items():
    for link in page.links:
        url = urlsplit(link)
        if url.scheme or url.netloc:
            continue
        local = unquote(url.path)
        if local.startswith(base + "/"):
            target = site / local[len(base) + 1:]
        elif local.startswith("/"):
            errors.append(f"{path}: link omits project base path: {link}")
            continue
        else:
            target = path.parent / local if local else path
        if target.is_dir():
            target /= "index.html"
        target = target.resolve()
        if not target.is_relative_to(site.resolve()) or not target.is_file():
            errors.append(f"{path}: missing local link {link}")
        elif url.fragment and target in pages and unquote(url.fragment) not in pages[target].ids:
            errors.append(f"{path}: missing anchor {link}")

reports = list((root / "docs/_experiments").glob("*.md"))
for report in reports:
    if not (site / "experiments" / report.stem / "index.html").exists():
        errors.append(f"Missing rendered report: {report.stem}")
for path in site.rglob("*"):
    if path.is_file() and ("artifacts" in path.parts or "superpowers" in path.parts or path.suffix in {".log", ".tfstate", ".pem", ".key"}):
        errors.append(f"Non-public artifact in output: {path}")
if errors:
    sys.exit("\n".join(errors))
print(f"Checked {len(files)} HTML pages, {len(reports)} reports, and all local links")
