"""Freeze the public site to a static directory, for GitHub Pages.

GitHub Pages serves static files, so the dynamic Flask app cannot run there. This walks the
sitemap — the same list of canonical, indexable URLs the app already advertises — renders each
one through the app's own test client, and writes it as a file. Nothing is re-implemented: the
pages come from the real routes and templates, so a frozen page and a served page are the same
bytes, and the "no intelligence logic in the web layer" rule is untouched.

A project site is published under a sub-path (``/<repo>/``) that GitHub Pages does not tell the
app about. Passing that path as ``SCRIPT_NAME`` makes ``url_for`` emit prefixed links, so the
frozen pages resolve their own assets and navigation without a post-hoc HTML rewrite.

Usage::

    PYTHONPATH=src python ops/export_static.py \
        --base-url https://<owner>.github.io/<repo> --out dist
"""

from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

#: Routes that only exist for a signed-in session, or that are operational rather than content.
#: They are not exported and their links are expected to be dead on the static mirror, so the
#: link check ignores them.
ACCOUNT_PREFIXES = (
    "/signin", "/signup", "/signout", "/saved", "/preferences", "/dashboard",
    "/watching", "/my-pipeline", "/alerts", "/notes", "/tags", "/admin", "/api",
    "/healthz",
)

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _destination(root: Path, path: str) -> Path:
    """Where a URL path lands inside the export.

    The export root maps to the site root (``/<repo>/`` for a project site), so the URL prefix
    is not repeated on disk. A path with a file extension keeps its name; every other path is a
    directory with an index.html, which is what a static host serves for a directory request.
    """
    clean = path.strip("/")
    if not clean:
        return root / "index.html"
    if "." in clean.rsplit("/", 1)[-1]:
        return root / clean
    return root / clean / "index.html"


def _is_page(path: str) -> bool:
    """Whether a path is a rendered page (rather than a static asset)."""
    return "." not in path.rsplit("/", 1)[-1] or path.endswith(".html")


def _internal_links(html: str, prefix: str) -> set[str]:
    """Same-site references in a rendered page, as export-relative paths.

    Both pages and assets are returned, so the caller can choose to crawl the former and verify
    the latter. External links, fragments and account routes are dropped.
    """
    import re

    found: set[str] = set()
    for raw in re.findall(r'(?:href|src)="([^"]+)"', html):
        if raw.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        path = raw.split("#", 1)[0].split("?", 1)[0]
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):] or "/"
        if any(rel == p or rel.startswith(p + "/") for p in ACCOUNT_PREFIXES):
            continue
        found.add(rel)
    return found


def export(base_url: str, out_dir: Path, limit: int | None = None) -> dict[str, int]:
    from oppintel.app.config import AppConfig
    from oppintel.app.main import create_app

    base_url = base_url.rstrip("/")
    prefix = urlparse(base_url).path.rstrip("/")

    cfg = AppConfig()
    cfg.base_url = base_url
    # The export is a build step, not a public request surface: run with the protections off
    # so bulk rendering is not rate-limited, and so no session or CSRF state is required.
    cfg.debug = True

    app = create_app(cfg)
    client = app.test_client()

    def get(path: str):
        return client.get(path, environ_overrides={"SCRIPT_NAME": prefix})

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # GitHub Pages runs Jekyll unless told not to; Jekyll would drop files that start with an
    # underscore and copy everything else through a filter we do not want.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    sitemap_xml = get("/sitemap.xml")
    if sitemap_xml.status_code != 200:
        raise SystemExit(f"sitemap.xml returned {sitemap_xml.status_code}; cannot enumerate pages")
    root = ET.fromstring(sitemap_xml.get_data(as_text=True))
    locations = [el.text for el in root.iter(f"{SITEMAP_NS}loc") if el.text]

    written = 0
    seen: set[str] = set()
    queue: list[str] = [loc[len(base_url):] or "/" for loc in locations]

    # Pages the sitemap omits but that other pages link to (the report detail pages) are picked
    # up by following same-site links from each rendered page. Account routes are filtered out,
    # so the crawl cannot wander into the signed-in area.
    while queue:
        path = queue.pop(0)
        if path in seen or any(
            path == p or path.startswith(p + "/") for p in ACCOUNT_PREFIXES
        ):
            continue
        if not _is_page(path):
            continue
        seen.add(path)
        if limit is not None and written >= limit:
            continue
        response = get(path)
        if response.status_code != 200:
            raise SystemExit(f"{path} returned {response.status_code}; refusing a partial export")
        html = response.get_data(as_text=True)
        target = _destination(out_dir, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        written += 1
        queue.extend(sorted(link for link in _internal_links(html, prefix) if _is_page(link) and link not in seen))

    # The export root maps to the site root, so a file at <out>/static is served at
    # <base-url>/static — which is exactly the URL url_for wrote into every page.
    static_src = Path(cfg.static_dir)
    static_dst = out_dir / "static"
    shutil.copytree(static_src, static_dst, dirs_exist_ok=True)

    # sitemap.xml and robots.txt are pages the sitemap cannot list; fetch them directly so the
    # exported site advertises itself to crawlers.
    for special in ("/sitemap.xml", "/robots.txt"):
        response = get(special)
        if response.status_code != 200:
            raise SystemExit(f"{special} returned {response.status_code}")
        _destination(out_dir, special).write_bytes(response.get_data())

    # A static host needs a real 404 document; render the app's own so the branding matches.
    not_found = get("/this-path-does-not-exist-404")
    if not_found.status_code != 404:
        raise SystemExit(f"expected a 404 from the app, got {not_found.status_code}")
    (out_dir / "404.html").write_bytes(not_found.get_data())

    return {"pages": written, "static_files": sum(1 for _ in static_dst.rglob("*") if _.is_file())}


def check_links(out_dir: Path, base_url: str) -> list[str]:
    """Same-site references that point at a file the export does not contain.

    Runs over the exported HTML rather than the live app, so it catches a page whose navigation
    was correct when served but resolves to a missing file once frozen. Assets are included, so a
    stylesheet written under the wrong path is caught here rather than in a browser.
    """
    prefix = urlparse(base_url).path.rstrip("/")
    broken: set[str] = set()

    for html_file in out_dir.rglob("*.html"):
        text = html_file.read_text(encoding="utf-8", errors="ignore")
        for rel in _internal_links(text, prefix):
            if not _destination(out_dir, rel).exists():
                broken.add(rel)
    return sorted(broken)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="public site root, e.g. https://owner.github.io/repo")
    parser.add_argument("--out", default="dist", help="output directory (default dist)")
    parser.add_argument("--limit", type=int, default=None,
                        help="export at most this many pages (for a quick smoke test)")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any internal link is broken (use in CI)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    stats = export(args.base_url, out_dir, limit=args.limit)
    print(f"exported {stats['pages']} pages and {stats['static_files']} static files to {out_dir}")

    broken = check_links(out_dir, args.base_url)
    if broken:
        print(f"{len(broken)} internal link(s) resolve to no file:")
        for path in broken:
            print(f"  {path}")
        return 1 if args.strict else 0
    print("link check: every internal link resolves to an exported file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
