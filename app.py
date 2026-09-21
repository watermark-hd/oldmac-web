import re
import smtplib
import sqlite3
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from flask import (
    Flask, Response, render_template, send_from_directory, g, abort, request,
    url_for, redirect,
)

from apps import APPS, APPS_BY_SLUG

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
XPI_DIR = BASE_DIR / "static" / "downloads"
RETROCERT_DIR = BASE_DIR / "static" / "retrocert"

app = Flask(__name__)
app.url_map.strict_slashes = False
SITE_URL = "https://oldmac.policy-log.jp"


def load_feedback_config():
    """Read instance/feedback.env (KEY=VALUE lines) if present. Missing file just
    means email + the admin page stay disabled; feedback is still saved to the DB."""
    cfg = {}
    path = BASE_DIR / "instance" / "feedback.env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


FEEDBACK_CFG = load_feedback_config()
APP_LIST = [a for a in APPS if a["category"] == "app"]

# When Japanese prose is hand-wrapped across source lines in a template, the
# browser collapses that newline (+ indentation) to a single space — visible and
# wrong mid-sentence in Japanese, which has no inter-word spaces. Rather than
# joining hundreds of lines by hand, strip the whitespace only where a source
# newline sits directly between two CJK characters. ASCII/English is never
# touched (it has no CJK chars), so bilingual pages stay correct.
_CJK = r"　-ヿ㐀-䶿一-鿿＀-￯"
_CJK_LINEBREAK = re.compile(rf"(?<=[{_CJK}])[ \t]*\r?\n[ \t]*(?=[{_CJK}])")


@app.after_request
def collapse_cjk_linebreaks(response):
    if (
        response.direct_passthrough
        or response.mimetype != "text/html"
        or request.endpoint == "admin_feedback"  # user messages there may hold real newlines
    ):
        return response
    try:
        body = response.get_data(as_text=True)
    except (UnicodeDecodeError, RuntimeError):
        return response
    collapsed = _CJK_LINEBREAK.sub("", body)
    if collapsed != body:
        response.set_data(collapsed)
    return response


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
        "form_ts": int(time.time()),
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_at TEXT NOT NULL,
            app_slug TEXT,
            rating INTEGER,
            message TEXT NOT NULL,
            contact_email TEXT,
            lang TEXT,
            user_agent TEXT,
            ip TEXT,
            handled INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crash_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submitted_at TEXT NOT NULL,
            app_slug TEXT NOT NULL,
            app_version TEXT,
            os_version TEXT,
            machine TEXT,
            log_text TEXT NOT NULL,
            ip TEXT,
            handled INTEGER NOT NULL DEFAULT 0
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


def get_recent_updates(min_shown=3, within_days=90):
    """Flatten every app's changelog into one dated feed for the top page — no
    separate "latest news" data to hand-maintain, it's just apps.py's existing
    per-app changelog entries. Only the newest entry per app is kept, so one
    app's multi-entry release day can't crowd the rest out of a short list.

    Shows every entry from the last `within_days` days, however many that is
    (no fixed cap — it grows and shrinks with how much has actually shipped
    lately). If fewer than `min_shown` are that fresh, pads with the next-
    oldest entries so at least `min_shown` are always visible; as a new entry
    ages into the fresh window, it naturally pushes the oldest padding entry
    out, one at a time."""
    latest_per_app = {}
    for a in APP_LIST:
        entries = a.get("changelog") or []
        if not entries:
            continue
        newest = max(entries, key=lambda e: e["date"])
        latest_per_app[a["slug"]] = {
            "app_slug": a["slug"],
            "app_name": a["name"],
            "app_name_en": a["name_en"],
            "date": newest["date"],
            "note": newest["note"],
            "note_en": newest["note_en"],
        }
    by_date = sorted(latest_per_app.values(), key=lambda e: e["date"], reverse=True)

    cutoff = (datetime.utcnow() - timedelta(days=within_days)).strftime("%Y-%m-%d")
    fresh = [e for e in by_date if e["date"] >= cutoff]
    return fresh if len(fresh) >= min_shown else by_date[:min_shown]


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
        recent_updates=get_recent_updates(),
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


@app.route("/retrocert/<path:filename>")
def retrocert_publisher(filename):
    """Static publisher endpoint for RetroCert (github.com/watermark-hd/RetroCert):
    serves the signed manifest.json + certs/ that RetroCert clients fetch from
    VPS_BASE_URL. send_from_directory rejects path traversal on its own."""
    return send_from_directory(RETROCERT_DIR, filename)


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


@app.route("/<lang>/feedback", strict_slashes=False)
@app.route("/feedback", defaults={"lang": "ja"}, strict_slashes=False)
def feedback(lang="ja"):
    if lang not in SUPPORTED_LANGS:
        abort(404)
    return render_template(
        "feedback.html",
        lang=lang,
        apps=APP_LIST,
        preset_app=request.args.get("app", ""),
        sent=request.args.get("sent") == "1",
    )


def _send_feedback_email(app_slug, rating, message, contact_email, lang, ua, ip):
    """Best-effort notification via Gmail SMTP. The row is already saved before
    this runs, so a failure here only means no push notification."""
    addr = FEEDBACK_CFG.get("GMAIL_ADDRESS")
    pw = FEEDBACK_CFG.get("GMAIL_APP_PASSWORD")
    to = FEEDBACK_CFG.get("FEEDBACK_TO", addr)
    if not (addr and pw and to):
        return
    stars = ("★" * rating + "☆" * (5 - rating)) if rating else "(no rating)"
    subject_app = app_slug or "site (general)"
    msg = EmailMessage()
    msg["Subject"] = f"[oldmac feedback] {subject_app} {stars}"
    msg["From"] = addr
    msg["To"] = to
    if contact_email:
        msg["Reply-To"] = contact_email
    msg.set_content(
        f"App:     {subject_app}\n"
        f"Rating:  {rating if rating else '-'} / 5\n"
        f"Lang:    {lang}\n"
        f"Contact: {contact_email or '-'}\n"
        f"IP:      {ip or '-'}\n"
        f"UA:      {ua or '-'}\n"
        f"Time:    {datetime.utcnow().isoformat()}Z\n"
        f"\n----- message -----\n{message}\n\n"
        f"Admin: {SITE_URL}/admin/feedback?key=(your key)\n"
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=12) as server:
        server.starttls()
        server.login(addr, pw)
        server.send_message(msg)


@app.route("/feedback/submit", methods=["POST"])
def feedback_submit():
    lang = request.form.get("lang", "ja")
    if lang not in SUPPORTED_LANGS:
        lang = "ja"

    next_url = request.form.get("next", "")
    safe_next = next_url if (next_url.startswith("/") and not next_url.startswith("//")) else ""

    def done():
        if safe_next:
            sep = "&" if "?" in safe_next else "?"
            return redirect(f"{safe_next}{sep}fb=thanks#feedback")
        return redirect(url_for("feedback", lang=lang, sent="1"))

    # Honeypot: a real browser leaves this empty. Pretend success, save nothing.
    if request.form.get("website", "").strip():
        return done()

    try:
        form_ts = int(request.form.get("t", "0"))
    except ValueError:
        form_ts = 0
    now = int(time.time())
    too_fast = bool(form_ts) and (now - form_ts) < 2
    too_old = bool(form_ts) and (now - form_ts) > 86400

    message = (request.form.get("message") or "").strip()
    app_slug = (request.form.get("app") or "").strip() or None
    if app_slug and app_slug not in APPS_BY_SLUG:
        app_slug = None
    contact_email = (request.form.get("contact_email") or "").strip() or None
    if contact_email and ("@" not in contact_email or len(contact_email) > 200):
        contact_email = None
    try:
        rating = int(request.form.get("rating", ""))
        if not 1 <= rating <= 5:
            rating = None
    except (ValueError, TypeError):
        rating = None

    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")
          .split(",")[0].strip())
    ua = request.headers.get("User-Agent", "")[:500]

    if not message or len(message) > 4000 or too_fast or too_old:
        return done()

    db = get_db()
    if ip:
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        recent = db.execute(
            "SELECT COUNT(*) FROM feedback WHERE ip = ? AND submitted_at > ?",
            (ip, cutoff),
        ).fetchone()[0]
        if recent >= 5:
            return done()

    db.execute(
        "INSERT INTO feedback (submitted_at, app_slug, rating, message, contact_email, lang, user_agent, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), app_slug, rating, message, contact_email, lang, ua, ip),
    )
    db.commit()
    try:
        _send_feedback_email(app_slug, rating, message, contact_email, lang, ua, ip)
    except Exception as exc:  # noqa: BLE001 - never let email break the response
        app.logger.warning("feedback email failed: %s", exc)
    return done()


@app.route("/admin/feedback", strict_slashes=False)
def admin_feedback():
    key = FEEDBACK_CFG.get("ADMIN_KEY")
    if not key or request.args.get("key") != key:
        abort(404)
    db = get_db()
    rows = db.execute(
        "SELECT id, submitted_at, app_slug, rating, message, contact_email, lang, user_agent, ip "
        "FROM feedback ORDER BY id DESC LIMIT 500"
    ).fetchall()
    return render_template("admin_feedback.html", rows=rows)


# ---------- クラッシュ報告(2026-09-22、AquaLink向けに追加) ----------
# アプリ側(Cocoa/Tiger)から、利用者が明示的に同意した場合だけ届く。
# 「勝手に送らない・内容を書き換えない」という本サイトの他機能と同じ方針で、
# アプリ側は送信前に必ず全文をユーザーに見せた上での一回ごとの同意を取る
# (詳細はAquaLink側のAppDelegate.mコメント参照)。ここではフィードバック
# 機能と全く同じ基盤(sqlite保存 + Gmail SMTPでのベストエフォート通知)を
# そのまま再利用しており、新しい秘密情報の追加は不要。

def _send_crash_email(app_slug, app_version, os_version, machine, log_text, ip):
    """フィードバックと同じくベストエフォート。行が先に保存されているので、
    ここが失敗してもプッシュ通知が来ないだけで実害は無い。"""
    addr = FEEDBACK_CFG.get("GMAIL_ADDRESS")
    pw = FEEDBACK_CFG.get("GMAIL_APP_PASSWORD")
    to = FEEDBACK_CFG.get("FEEDBACK_TO", addr)
    if not (addr and pw and to):
        return
    msg = EmailMessage()
    msg["Subject"] = f"[oldmac crash] {app_slug} {app_version or '?'} — {machine or '?'}"
    msg["From"] = addr
    msg["To"] = to
    msg.set_content(
        f"App:     {app_slug}\n"
        f"Version: {app_version or '-'}\n"
        f"OS:      {os_version or '-'}\n"
        f"Machine: {machine or '-'}\n"
        f"IP:      {ip or '-'}\n"
        f"Time:    {datetime.utcnow().isoformat()}Z\n"
        f"\n----- crash log -----\n{log_text}\n\n"
        f"Admin: {SITE_URL}/admin/crashes?key=(your key)\n"
    )
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=12) as server:
        server.starttls()
        server.login(addr, pw)
        server.send_message(msg)


@app.route("/crash-report/submit", methods=["POST"])
def crash_report_submit():
    app_slug = (request.form.get("app") or "").strip()
    if app_slug not in APPS_BY_SLUG:
        abort(400)

    app_version = (request.form.get("app_version") or "").strip()[:50]
    os_version = (request.form.get("os_version") or "").strip()[:100]
    machine = (request.form.get("machine") or "").strip()[:100]
    log_text = request.form.get("log") or ""

    # クラッシュログは大きくてもだいたい100〜200KB程度(実機で確認した範囲)。
    # 上限を大きめに300KBに設定し、それ以上/空は不正なリクエストとして拒否する。
    if not log_text or len(log_text) > 300_000:
        abort(400)

    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")
          .split(",")[0].strip())

    db = get_db()
    if ip:
        # フィードバックより緩め(1時間10件)。連続クラッシュを何度か報告する
        # ケースは正当にあり得るため。
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        recent = db.execute(
            "SELECT COUNT(*) FROM crash_reports WHERE ip = ? AND submitted_at > ?",
            (ip, cutoff),
        ).fetchone()[0]
        if recent >= 10:
            return ("rate limited", 429)

    db.execute(
        "INSERT INTO crash_reports (submitted_at, app_slug, app_version, os_version, machine, log_text, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), app_slug, app_version, os_version, machine, log_text, ip),
    )
    db.commit()
    try:
        _send_crash_email(app_slug, app_version, os_version, machine, log_text, ip)
    except Exception as exc:  # noqa: BLE001 - never let email break the response
        app.logger.warning("crash report email failed: %s", exc)
    return ("OK", 200)


@app.route("/admin/crashes", strict_slashes=False)
def admin_crashes():
    key = FEEDBACK_CFG.get("ADMIN_KEY")
    if not key or request.args.get("key") != key:
        abort(404)
    db = get_db()
    rows = db.execute(
        "SELECT id, submitted_at, app_slug, app_version, os_version, machine, log_text, ip "
        "FROM crash_reports ORDER BY id DESC LIMIT 200"
    ).fetchall()
    return render_template("admin_crashes.html", rows=rows)


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
        "/ja/apps/retrocert/", "/en/apps/retrocert/",
        "/ja/apps/ppc-trackpad-scroll/", "/en/apps/ppc-trackpad-scroll/",
        "/ja/apps/advisor/", "/en/apps/advisor/",
        "/ja/articles/why-old-macs/", "/en/articles/why-old-macs/",
        "/ja/feedback/", "/en/feedback/",
    ]
    entries = "".join(f"  <url><loc>{SITE_URL}{path}</loc></url>\n" for path in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{entries}</urlset>\n'
    return Response(xml, mimetype="application/xml")


if __name__ == "__main__":
    app.run(debug=True, port=5001)
