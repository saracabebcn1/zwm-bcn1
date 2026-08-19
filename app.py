import os
import sqlite3
import uuid
from datetime import datetime
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, g, send_from_directory, abort)
from werkzeug.utils import secure_filename
from PIL import Image

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "zwm-bcn1-secret-2026")

ADMIN_CODE = os.environ.get("ADMIN_CODE", "BCN1admin2026")
DB_PATH = os.environ.get("DB_PATH", "zwm.db")
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
MAX_PHOTOS = 3
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_IMG_SIZE = (1200, 1200)

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            category_id INTEGER REFERENCES categories(id),
            description TEXT,
            measurements TEXT,
            status      TEXT NOT NULL DEFAULT 'available',
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS photos (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id  INTEGER REFERENCES items(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            position INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER REFERENCES items(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            alias      TEXT NOT NULL,
            department TEXT NOT NULL,
            message    TEXT,
            created_at TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending'
        );
    """)
    # seed default categories
    defaults = ["Sillas","Mesas","Estanterías","Armarios","Carros",
                "Monitores","Equipos IT","Iluminación","Otros"]
    for cat in defaults:
        db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cat,))
    db.commit()

# ── helpers ──────────────────────────────────────────────────────────────────

def allowed(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def save_photo(file):
    ext = file.filename.rsplit(".", 1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_FOLDER, fname)
    img = Image.open(file)
    img.thumbnail(MAX_IMG_SIZE, Image.LANCZOS)
    img.save(path, optimize=True, quality=85)
    return fname

def is_admin():
    return session.get("admin") is True

# ── routes: public ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    cat_id = request.args.get("cat", "")
    status_filter = request.args.get("status", "available")
    query = """
        SELECT i.*, c.name AS cat_name,
               (SELECT filename FROM photos WHERE item_id=i.id ORDER BY position LIMIT 1) AS cover
        FROM items i LEFT JOIN categories c ON c.id=i.category_id
        WHERE 1=1
    """
    params = []
    if cat_id:
        query += " AND i.category_id=?"
        params.append(cat_id)
    if status_filter != "all":
        query += " AND i.status=?"
        params.append(status_filter)
    query += " ORDER BY i.created_at DESC"
    items = db.execute(query, params).fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    counts = {r["status"]: r["n"] for r in
              db.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()}
    return render_template("index.html", items=items, categories=categories,
                           active_cat=cat_id, active_status=status_filter, counts=counts)

@app.route("/item/<int:item_id>")
def item_detail(item_id):
    db = get_db()
    item = db.execute(
        "SELECT i.*, c.name AS cat_name FROM items i LEFT JOIN categories c ON c.id=i.category_id WHERE i.id=?",
        (item_id,)).fetchone()
    if not item:
        abort(404)
    photos = db.execute("SELECT * FROM photos WHERE item_id=? ORDER BY position", (item_id,)).fetchall()
    ticket_count = db.execute("SELECT COUNT(*) n FROM tickets WHERE item_id=?", (item_id,)).fetchone()["n"]
    return render_template("item_detail.html", item=item, photos=photos, ticket_count=ticket_count)

@app.route("/ticket/<int:item_id>", methods=["GET", "POST"])
def ticket(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    if item["status"] != "available":
        flash("Este artículo ya no está disponible.", "warning")
        return redirect(url_for("item_detail", item_id=item_id))
    if request.method == "POST":
        name  = request.form.get("name", "").strip()
        alias = request.form.get("alias", "").strip()
        dept  = request.form.get("department", "").strip()
        msg   = request.form.get("message", "").strip()
        if not name or not alias or not dept:
            flash("Rellena los campos obligatorios.", "danger")
        else:
            db.execute(
                "INSERT INTO tickets(item_id,name,alias,department,message,created_at,status) VALUES(?,?,?,?,?,?,?)",
                (item_id, name, alias, dept, msg, datetime.now().isoformat(timespec="seconds"), "pending"))
            # Auto-reservar el artículo al recibir la primera solicitud
            db.execute("UPDATE items SET status='reserved' WHERE id=? AND status='available'", (item_id,))
            db.commit()
            flash("✅ Solicitud enviada. El equipo de gestión se pondrá en contacto contigo.", "success")
            return redirect(url_for("item_detail", item_id=item_id))
    return render_template("ticket.html", item=item)

# ── routes: admin ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
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
    db = get_db()
    items = db.execute("""
        SELECT i.*, c.name cat_name,
               (SELECT filename FROM photos WHERE item_id=i.id ORDER BY position LIMIT 1) cover,
               (SELECT COUNT(*) FROM tickets WHERE item_id=i.id AND status='pending') pending_tickets
        FROM items i LEFT JOIN categories c ON c.id=i.category_id
        ORDER BY i.created_at DESC
    """).fetchall()
    categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    tickets = db.execute("""
        SELECT t.*, i.title item_title FROM tickets t
        JOIN items i ON i.id=t.item_id
        ORDER BY t.created_at DESC
    """).fetchall()
    return render_template("admin.html", items=items, categories=categories, tickets=tickets)

@app.route("/admin/publish", methods=["GET", "POST"])
def publish():
    if not is_admin():
        return redirect(url_for("admin_login"))
    db = get_db()
    categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        cat_id   = request.form.get("category_id")
        desc     = request.form.get("description", "").strip()
        measures = request.form.get("measurements", "").strip()
        if not title:
            flash("El título es obligatorio.", "danger")
            return render_template("publish.html", categories=categories)
        cur = db.execute(
            "INSERT INTO items(title,category_id,description,measurements,status,created_at) VALUES(?,?,?,?,?,?)",
            (title, cat_id or None, desc, measures, "available",
             datetime.now().isoformat(timespec="seconds")))
        item_id = cur.lastrowid
        pos = 0
        for key in ["photo1", "photo2", "photo3"]:
            f = request.files.get(key)
            if f and f.filename and allowed(f.filename):
                fname = save_photo(f)
                db.execute("INSERT INTO photos(item_id,filename,position) VALUES(?,?,?)",
                           (item_id, fname, pos))
                pos += 1
        db.commit()
        flash("✅ Artículo publicado.", "success")
        return redirect(url_for("admin_panel"))
    return render_template("publish.html", categories=categories)

@app.route("/admin/item/<int:item_id>/status", methods=["POST"])
def set_status(item_id):
    if not is_admin():
        abort(403)
    new_status = request.form.get("status")
    if new_status in ("available", "reserved", "in_progress", "delivered"):
        get_db().execute("UPDATE items SET status=? WHERE id=?", (new_status, item_id))
        get_db().commit()
    return redirect(request.referrer or url_for("admin_panel"))

@app.route("/admin/item/<int:item_id>/delete", methods=["POST"])
def delete_item(item_id):
    if not is_admin():
        abort(403)
    db = get_db()
    photos = db.execute("SELECT filename FROM photos WHERE item_id=?", (item_id,)).fetchall()
    for p in photos:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, p["filename"]))
        except FileNotFoundError:
            pass
    db.execute("DELETE FROM items WHERE id=?", (item_id,))
    db.commit()
    flash("Artículo eliminado.", "info")
    return redirect(url_for("admin_panel"))

@app.route("/admin/ticket/<int:ticket_id>/close", methods=["POST"])
def close_ticket(ticket_id):
    if not is_admin():
        abort(403)
    get_db().execute("UPDATE tickets SET status='processed' WHERE id=?", (ticket_id,))
    get_db().commit()
    return redirect(url_for("admin_panel"))

@app.route("/admin/category/add", methods=["POST"])
def add_category():
    if not is_admin():
        abort(403)
    name = request.form.get("name", "").strip()
    if name:
        try:
            get_db().execute("INSERT INTO categories(name) VALUES(?)", (name,))
            get_db().commit()
        except Exception:
            flash("Esa categoría ya existe.", "warning")
    return redirect(url_for("admin_panel"))

@app.route("/admin/category/<int:cat_id>/delete", methods=["POST"])
def delete_category(cat_id):
    if not is_admin():
        abort(403)
    get_db().execute("DELETE FROM categories WHERE id=?", (cat_id,))
    get_db().commit()
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

@app.context_processor
def inject_now():
    return {"now": datetime.now()}
