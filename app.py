import sqlite3
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, render_template, send_from_directory, g, abort, request, url_for

from apps import APPS, APPS_BY_SLUG

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
XPI_DIR = BASE_DIR / "static" / "downloads"

app = Flask(__name__)
app.url_map.strict_slashes = False
SITE_URL = "https://oldmac.policy-log.jp"


def _lang_path(lang, endpoint, view_args):
    path = url_for(endpoint, lang=lang, **view_args)
    return path if path.endswith("/") else f"{path}/"


@app.context_processor
def inject_site_metadata():
    """Provide canonical/hreflang URLs derived from the current route, not the raw path."""
    view_args = dict(request.view_args or {})
    current_lang = view_args.pop("lang", "ja")
    endpoint = request.endpoint
    ja_url = f"{SITE_URL}{_lang_path('ja', endpoint, view_args)}"
    en_url = f"{SITE_URL}{_lang_path('en', endpoint, view_args)}"
    return {
        "canonical_url": ja_url if current_lang == "ja" else en_url,
        "alternate_ja_url": ja_url,
        "alternate_en_url": en_url,
    }


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_slug TEXT NOT NULL,
            downloaded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def get_download_counts():
    db = get_db()
    rows = db.execute(
        "SELECT app_slug, COUNT(*) FROM downloads GROUP BY app_slug"
    ).fetchall()
    counts = {app["slug"]: 0 for app in APPS}
    counts.update(dict(rows))
    return counts


def get_updated_at(filename):
    """Derive a "last updated" date straight from the distributed file's mtime,
    so it can't drift out of sync the way a hand-maintained date field would."""
    if not filename:
        return None
    path = XPI_DIR / filename
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


SUPPORTED_LANGS = {"ja", "en"}


@app.route("/<lang>/", strict_slashes=False)
@app.route("/<lang>", strict_slashes=False)
@app.route("/", defaults={"lang": "ja"}, strict_slashes=False)
def top(lang="ja"):
    if lang not in SUPPORTED_LANGS:
        abort(404)
    return render_template(
        "top.html",
        lang=lang,
        apps=APPS,
        download_counts=get_download_counts(),
        updated_ats={a["slug"]: get_updated_at(a["filename"]) for a in APPS},
    )


@app.route("/apps/<slug>", defaults={"lang": "ja"}, strict_slashes=False)
@app.route("/<lang>/apps/<slug>", strict_slashes=False)
def app_detail(lang, slug):
    if lang not in SUPPORTED_LANGS or slug not in APPS_BY_SLUG:
        abort(404)
    return render_template(
        f"apps/{slug}.html",
        lang=lang,
        app=APPS_BY_SLUG[slug],
        download_count=get_download_counts()[slug],
        updated_at=get_updated_at(APPS_BY_SLUG[slug]["filename"]),
    )


@app.route("/download/<slug>", defaults={"lang": "ja"}, strict_slashes=False)
@app.route("/<lang>/download/<slug>", strict_slashes=False)
def download(lang, slug):
    if lang not in SUPPORTED_LANGS:
        abort(404)
    if slug not in APPS_BY_SLUG or not APPS_BY_SLUG[slug]["filename"]:
        abort(404)
    db = get_db()
    db.execute(
        "INSERT INTO downloads (app_slug, downloaded_at) VALUES (?, ?)",
        (slug, datetime.utcnow().isoformat()),
    )
    db.commit()
    return send_from_directory(XPI_DIR, APPS_BY_SLUG[slug]["filename"], as_attachment=True)


@app.route("/<lang>/about", strict_slashes=False)
@app.route("/about", defaults={"lang": "ja"}, strict_slashes=False)
def about(lang="ja"):
    if lang not in SUPPORTED_LANGS:
        abort(404)
    return render_template("about.html", lang=lang)


@app.route("/<lang>/articles/why-old-macs", strict_slashes=False)
@app.route("/articles/why-old-macs", defaults={"lang": "ja"}, strict_slashes=False)
def article_why_old_macs(lang="ja"):
    if lang not in SUPPORTED_LANGS:
        abort(404)
    return render_template("articles/why-old-macs.html", lang=lang)


@app.route("/robots.txt")
def robots():
    return Response(
        f"User-agent: *\nAllow: /\n\nSitemap: {SITE_URL}/sitemap.xml\n",
        mimetype="text/plain",
    )


@app.route("/googlebf17354c49a90927.html")
def google_site_verification():
    return send_from_directory(BASE_DIR, "googlebf17354c49a90927.html")


@app.route("/sitemap.xml")
def sitemap():
    """List indexable pages for search engines; exclude unpublished projects."""
    paths = [
        "/ja/", "/en/", "/ja/about/", "/en/about/",
        "/ja/apps/aquafox-ja/", "/en/apps/aquafox-ja/",
        "/ja/apps/aquafinder/", "/en/apps/aquafinder/",
        "/ja/apps/aqualink/", "/en/apps/aqualink/",
        "/ja/apps/ppc-claude-agent/", "/en/apps/ppc-claude-agent/",
        "/ja/apps/exfat-tiger-ppc/", "/en/apps/exfat-tiger-ppc/",
        "/ja/apps/kodama/", "/en/apps/kodama/",
        "/ja/apps/tiger-quicklook/", "/en/apps/tiger-quicklook/",
        "/ja/articles/why-old-macs/", "/en/articles/why-old-macs/",
    ]
    entries = "".join(f"  <url><loc>{SITE_URL}{path}</loc></url>\n" for path in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n'
    return Response(xml, mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
