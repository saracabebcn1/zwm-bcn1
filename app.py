import os
import io
import uuid
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, g, send_from_directory, abort)
from werkzeug.utils import secure_filename
from PIL import Image
import psycopg2
import psycopg2.extras
import requests as http_client

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "zwm-bcn1-secret-2026")

ADMIN_CODE      = os.environ.get("ADMIN_CODE", "BCN1admin2026")
DATABASE_URL    = os.environ.get("DATABASE_URL", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY    = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = "Photos"

UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
ALLOWED_EXT   = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_IMG_SIZE  = (1200, 1200)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        import urllib.parse
        p = urllib.parse.urlparse(DATABASE_URL)
        g.db = psycopg2.connect(
            host=p.hostname,
            port=p.port or 5432,
            user=p.username,
            password=p.password,
            dbname=(p.path or "/postgres").lstrip("/") or "postgres",
            sslmode="require",
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

def q(sql, params=(), one=False, commit=False):
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    if commit:
        db.commit()
        return cur
    return cur.fetchone() if one else cur.fetchall()

def init_db():
    db  = get_db()
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id   SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id           SERIAL PRIMARY KEY,
            title        TEXT NOT NULL,
            category_id  INTEGER REFERENCES categories(id),
            description  TEXT,
            measurements TEXT,
            status       TEXT NOT NULL DEFAULT 'available',
            created_at   TEXT NOT NULL
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id       SERIAL PRIMARY KEY,
            item_id  INTEGER REFERENCES items(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            position INTEGER DEFAULT 0
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id         SERIAL PRIMARY KEY,
            item_id    INTEGER REFERENCES items(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            alias      TEXT NOT NULL,
            department TEXT NOT NULL,
            message    TEXT,
            created_at TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending'
        )""")
    defaults = ["Sillas","Mesas","Estanterías","Armarios","Carros",
                "Monitores","Equipos IT","Iluminación","Contenedores","Otros"]
    for cat in defaults:
        cur.execute("INSERT INTO categories(name) VALUES(%s) ON CONFLICT DO NOTHING", (cat,))
    db.commit()

# ── helpers ───────────────────────────────────────────────────────────────────

def allowed(filename):
    return "." in filename and filename.rsplit(".",1)[1].lower() in ALLOWED_EXT

def save_photo(file):
    """Sube foto a Supabase Storage y devuelve la URL pública (o nombre local como fallback)."""
    ext  = file.filename.rsplit(".", 1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"

    img = Image.open(file)
    img.thumbnail(MAX_IMG_SIZE, Image.LANCZOS)

    if SUPABASE_URL and SUPABASE_KEY:
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF"}
        ct_map  = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                   "webp": "image/webp", "gif": "image/gif"}
        buf = io.BytesIO()
        img.save(buf, format=fmt_map.get(ext, "JPEG"), optimize=True, quality=85)
        buf.seek(0)
        raw = buf.read()
        r = http_client.post(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{fname}",
            headers={"Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": ct_map.get(ext, "image/jpeg"),
                     "x-upsert": "true"},
            data=raw
        )
        if not r.ok:
            app.logger.error(f"Supabase upload error {r.status_code}: {r.text}")
            raise Exception(f"Error subiendo foto: {r.status_code} {r.text}")
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{fname}"
    else:
        # Fallback local (dev)
        path = os.path.join(UPLOAD_FOLDER, fname)
        img.save(path, optimize=True, quality=85)
        return fname

def delete_photo(filename_or_url):
    """Elimina foto de Supabase Storage o del disco local."""
    if SUPABASE_URL and SUPABASE_KEY and filename_or_url.startswith("http"):
        fname = filename_or_url.split(f"/object/public/{SUPABASE_BUCKET}/")[-1]
        try:
            http_client.delete(
                f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{fname}",
                headers={"Authorization": f"Bearer {SUPABASE_KEY}"}
            )
        except Exception:
            pass
    else:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, filename_or_url))
        except FileNotFoundError:
            pass

def is_admin():
    return session.get("admin") is True

# ── category styles ───────────────────────────────────────────────────────────

CAT_STYLE = {
    "Sillas":       {"icon": "bi-chair",     "bg": "#e8f5e9", "color": "#2e7d32"},
    "Mesas":        {"icon": "bi-table",      "bg": "#fff8e1", "color": "#f57f17"},
    "Estanterías":  {"icon": "bi-bookshelf",  "bg": "#e3f2fd", "color": "#1565c0"},
    "Armarios":     {"icon": "bi-archive",    "bg": "#f3e5f5", "color": "#6a1b9a"},
    "Carros":       {"icon": "bi-cart3",      "bg": "#e8eaf6", "color": "#283593"},
    "Monitores":    {"icon": "bi-display",    "bg": "#fce4ec", "color": "#c62828"},
    "Equipos IT":   {"icon": "bi-laptop",     "bg": "#e0f7fa", "color": "#00695c"},
    "Iluminación":  {"icon": "bi-lightbulb",  "bg": "#fff3e0", "color": "#e65100"},
    "Contenedores": {"icon": "bi-box-seam",   "bg": "#f1f8e9", "color": "#558b2f"},
    "Otros":        {"icon": "bi-three-dots", "bg": "#f5f5f5", "color": "#555"},
}
DEFAULT_STYLE = {"icon": "bi-grid", "bg": "#f5f5f5", "color": "#555"}

# ── context processor ─────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    def photo_url(filename):
        if not filename:
            return ""
        if filename.startswith("http"):
            return filename
        return url_for("uploaded_file", filename=filename)
    def cat_style(name):
        return CAT_STYLE.get(name, DEFAULT_STYLE)
    return {"photo_url": photo_url, "cat_style": cat_style, "now": datetime.now()}

# ── routes: public ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    categories   = q("SELECT * FROM categories ORDER BY name")
    raw_avail    = q("SELECT category_id, COUNT(*) AS n FROM items WHERE status='available' GROUP BY category_id")
    avail_by_cat = {str(r["category_id"]): r["n"] for r in raw_avail}
    raw_counts   = q("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
    counts       = {r["status"]: r["n"] for r in raw_counts}
    return render_template("index.html", categories=categories,
                           avail_by_cat=avail_by_cat, counts=counts)

@app.route("/categoria/<int:cat_id>")
def category_items(cat_id):
    cat = q("SELECT * FROM categories WHERE id=%s", (cat_id,), one=True)
    if not cat:
        abort(404)
    status_filter = request.args.get("status", "available")
    page          = max(1, int(request.args.get("page", 1)))
    per_page      = 12

    base_sql = """
        SELECT i.*,
               (SELECT filename FROM photos WHERE item_id=i.id ORDER BY position LIMIT 1) AS cover
        FROM items i WHERE i.category_id=%s
    """
    count_sql = "SELECT COUNT(*) AS n FROM items WHERE category_id=%s"
    params = [cat_id]
    if status_filter != "all":
        base_sql  += " AND i.status=%s"
        count_sql += " AND status=%s"
        params.append(status_filter)

    total       = q(count_sql, params, one=True)["n"]
    total_pages = max(1, (total + per_page - 1) // per_page)
    page        = min(page, total_pages)
    offset      = (page - 1) * per_page
    items       = q(base_sql + " ORDER BY i.created_at DESC LIMIT %s OFFSET %s",
                    params + [per_page, offset])
    raw_sc      = q("SELECT status, COUNT(*) AS n FROM items WHERE category_id=%s GROUP BY status", (cat_id,))
    status_counts = {r["status"]: r["n"] for r in raw_sc}
    return render_template("category.html", cat=cat, items=items,
                           active_status=status_filter, page=page,
                           total_pages=total_pages, status_counts=status_counts)

@app.route("/item/<int:item_id>")
def item_detail(item_id):
    item = q("""SELECT i.*, c.name AS cat_name
                FROM items i LEFT JOIN categories c ON c.id=i.category_id
                WHERE i.id=%s""", (item_id,), one=True)
    if not item:
        abort(404)
    photos       = q("SELECT * FROM photos WHERE item_id=%s ORDER BY position", (item_id,))
    ticket_count = q("SELECT COUNT(*) AS n FROM tickets WHERE item_id=%s", (item_id,), one=True)["n"]
    return render_template("item_detail.html", item=item, photos=photos, ticket_count=ticket_count)

@app.route("/ticket/<int:item_id>", methods=["GET","POST"])
def ticket(item_id):
    item = q("SELECT * FROM items WHERE id=%s", (item_id,), one=True)
    if not item:
        abort(404)
    if item["status"] != "available":
        flash("Este artículo ya no está disponible.", "warning")
        return redirect(url_for("item_detail", item_id=item_id))
    if request.method == "POST":
        name  = request.form.get("name","").strip()
        alias = request.form.get("alias","").strip()
        dept  = request.form.get("department","").strip()
        msg   = request.form.get("message","").strip()
        if not name or not alias or not dept:
            flash("Rellena los campos obligatorios.", "danger")
        else:
            q("""INSERT INTO tickets(item_id,name,alias,department,message,created_at,status)
                 VALUES(%s,%s,%s,%s,%s,%s,%s)""",
              (item_id, name, alias, dept, msg,
               datetime.now().isoformat(timespec="seconds"), "pending"), commit=True)
            q("UPDATE items SET status='reserved' WHERE id=%s AND status='available'",
              (item_id,), commit=True)
            flash("✅ Solicitud enviada. El equipo de gestión se pondrá en contacto contigo.", "success")
            return redirect(url_for("item_detail", item_id=item_id))
    return render_template("ticket.html", item=item)

# ── routes: admin ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("code") == ADMIN_CODE:
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        flash("Código incorrecto.", "danger")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/admin")
def admin_panel():
    if not is_admin():
        return redirect(url_for("admin_login"))
    items = q("""
        SELECT i.*, c.name cat_name,
               (SELECT filename FROM photos WHERE item_id=i.id ORDER BY position LIMIT 1) cover,
               (SELECT COUNT(*) FROM tickets WHERE item_id=i.id AND status='pending') pending_tickets
        FROM items i LEFT JOIN categories c ON c.id=i.category_id
        ORDER BY i.created_at DESC""")
    categories = q("SELECT * FROM categories ORDER BY name")
    tickets    = q("""SELECT t.*, i.title item_title FROM tickets t
                      JOIN items i ON i.id=t.item_id
                      ORDER BY t.created_at DESC""")
    return render_template("admin.html", items=items, categories=categories, tickets=tickets)

@app.route("/admin/publish", methods=["GET","POST"])
def publish():
    if not is_admin():
        return redirect(url_for("admin_login"))
    categories = q("SELECT * FROM categories ORDER BY name")
    if request.method == "POST":
        title    = request.form.get("title","").strip()
        cat_id   = request.form.get("category_id") or None
        desc     = request.form.get("description","").strip()
        measures = request.form.get("measurements","").strip()
        if not title or not cat_id:
            flash("El título y la categoría son obligatorios.", "danger")
            return render_template("publish.html", categories=categories)
        cur = get_db().cursor()
        cur.execute("""INSERT INTO items(title,category_id,description,measurements,status,created_at)
                       VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (title, cat_id, desc, measures, "available",
                     datetime.now().isoformat(timespec="seconds")))
        item_id = cur.fetchone()["id"]
        get_db().commit()
        pos = 0
        for key in ["photo1","photo2","photo3"]:
            f = request.files.get(key)
            if f and f.filename and allowed(f.filename):
                fname = save_photo(f)
                q("INSERT INTO photos(item_id,filename,position) VALUES(%s,%s,%s)",
                  (item_id, fname, pos), commit=True)
                pos += 1
        flash("✅ Artículo publicado.", "success")
        return redirect(url_for("admin_panel"))
    return render_template("publish.html", categories=categories)

@app.route("/admin/item/<int:item_id>/status", methods=["POST"])
def set_status(item_id):
    if not is_admin(): abort(403)
    new_status = request.form.get("status")
    if new_status in ("available","reserved","in_progress","delivered"):
        q("UPDATE items SET status=%s WHERE id=%s", (new_status, item_id), commit=True)
    return redirect(request.referrer or url_for("admin_panel"))

@app.route("/admin/item/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    if not is_admin(): abort(403)
    photos = q("SELECT filename FROM photos WHERE item_id=%s", (item_id,))
    for p in photos:
        delete_photo(p["filename"])
    q("DELETE FROM items WHERE id=%s", (item_id,), commit=True)
    flash("Artículo eliminado.", "info")
    return redirect(url_for("admin_panel"))

@app.route("/admin/ticket/<int:ticket_id>/close", methods=["POST"])
def close_ticket(ticket_id):
    if not is_admin(): abort(403)
    q("UPDATE tickets SET status='processed' WHERE id=%s", (ticket_id,), commit=True)
    return redirect(url_for("admin_panel"))

@app.route("/admin/category/add", methods=["POST"])
def add_category():
    if not is_admin(): abort(403)
    name = request.form.get("name","").strip()
    if name:
        try: q("INSERT INTO categories(name) VALUES(%s)", (name,), commit=True)
        except Exception: flash("Esa categoría ya existe.", "warning")
    return redirect(url_for("admin_panel"))

@app.route("/admin/category/<int:cat_id>/delete", methods=["POST"])
def delete_category(cat_id):
    if not is_admin(): abort(403)
    q("DELETE FROM categories WHERE id=%s", (cat_id,), commit=True)
    return redirect(url_for("admin_panel"))

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# ── boot ──────────────────────────────────────────────────────────────────────

with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
