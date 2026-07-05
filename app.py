"""
نظام مراقبة دقة إضافة الصوصات - التطبيق الكامل (Backend)
======================================================
تطبيق Flask حقيقي مع قاعدة بيانات SQLite فعلية (مو بيانات وهمية).
يعرض لوحة تحكم حية، ويستقبل نتائج التحقق عبر API حقيقي.

للتشغيل محلياً:
    pip install flask
    python app.py
    افتح المتصفح على: http://localhost:5000

جاهز للنشر على منصات مجانية مثل Render.com (انظر ملف DEPLOY.md).
"""
from flask import Flask, jsonify, request, render_template, send_file
import sqlite3
import os
import random
import json
import cv2
import numpy as np
from datetime import datetime

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024  # حد أقصى 150 ميجا (لدعم مقاطع أطول 3-5 دقائق)
DB_PATH = os.path.join(os.path.dirname(__file__), "sauce_monitor.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploaded_videos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# نطاقات الكشف اللوني - معايَرة على صور وفيديوهات حقيقية (انظر القسم 7 و17 بالمستند التقني)
RED_RANGE = {"hue_max_low": 8, "hue_min_high": 172, "sat_min": 190, "val_min": 50}
WHITE_RANGE = {"sat_max": 45, "val_min": 150}
RED_DETECTION_THRESHOLD = 150
WHITE_DETECTION_THRESHOLD = 3000


# ---------------------------------------------------------------------
# قاعدة البيانات
# ---------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS employees (
        employee_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS product_catalog (
        product_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        is_base_ingredient INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS orders (
        order_id INTEGER PRIMARY KEY AUTOINCREMENT,
        pos_order_number TEXT NOT NULL,
        required_sauces TEXT NOT NULL,  -- JSON list كنص
        employee_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(employee_id) REFERENCES employees(employee_id)
    );

    CREATE TABLE IF NOT EXISTS verifications (
        verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        detected_sauces TEXT NOT NULL,  -- JSON list كنص
        result TEXT NOT NULL,           -- 'match' أو 'mismatch'
        missing_items TEXT,             -- JSON list كنص
        verified_at TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(order_id)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
        verification_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        FOREIGN KEY(verification_id) REFERENCES verifications(verification_id)
    );

    CREATE TABLE IF NOT EXISTS video_uploads (
        video_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stored_filename TEXT NOT NULL,
        original_filename TEXT NOT NULL,
        employee TEXT,
        required_sauces TEXT,
        detected_sauces TEXT,
        result TEXT,
        frames_analyzed INTEGER,
        max_red INTEGER,
        max_white INTEGER,
        file_size_bytes INTEGER,
        uploaded_at TEXT NOT NULL
    );
    """)

    # بذر بيانات أساسية إن كانت الجداول فارغة
    if cur.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
        for name in ["أحمد", "سارة", "محمد", "نورة"]:
            cur.execute("INSERT INTO employees (name) VALUES (?)", (name,))

    if cur.execute("SELECT COUNT(*) FROM product_catalog").fetchone()[0] == 0:
        sauces = [("كاتشب", 0), ("ثوم", 0), ("كوكتيل", 0), ("بافلو", 0), ("جبنة", 1), ("ديناميت", 0)]
        cur.executemany("INSERT INTO product_catalog (name, is_base_ingredient) VALUES (?, ?)", sauces)

    conn.commit()
    conn.close()


def compute_result(required, detected):
    req, det = set(required), set(detected)
    missing = sorted(req - det)
    extra = sorted(det - req)
    result = "match" if not missing and not extra else "mismatch"
    return result, missing, extra


def record_verification(employee_name, required, detected, order_number=None):
    """تسجّل نتيجة تحقق بقاعدة البيانات - تُستخدم من /api/verify و/api/analyze_video معاً."""
    conn = get_db()
    cur = conn.cursor()

    emp_row = cur.execute("SELECT employee_id FROM employees WHERE name = ?", (employee_name,)).fetchone()
    if emp_row is None:
        cur.execute("INSERT INTO employees (name) VALUES (?)", (employee_name,))
        employee_id = cur.lastrowid
    else:
        employee_id = emp_row["employee_id"]

    now = datetime.now().isoformat()
    if not order_number:
        order_number = f"AUTO-{random.randint(1000,9999)}"

    cur.execute(
        "INSERT INTO orders (pos_order_number, required_sauces, employee_id, created_at) VALUES (?, ?, ?, ?)",
        (order_number, json.dumps(required, ensure_ascii=False), employee_id, now),
    )
    order_id = cur.lastrowid

    result, missing, extra = compute_result(required, detected)
    cur.execute(
        "INSERT INTO verifications (order_id, detected_sauces, result, missing_items, verified_at) VALUES (?, ?, ?, ?, ?)",
        (order_id, json.dumps(detected, ensure_ascii=False), result, json.dumps(missing, ensure_ascii=False), now),
    )
    verification_id = cur.lastrowid

    if result == "mismatch":
        cur.execute(
            "INSERT INTO alerts (verification_id, status, created_at) VALUES (?, 'open', ?)",
            (verification_id, now),
        )

    conn.commit()
    conn.close()
    return order_id, result, missing, extra


# ---------------------------------------------------------------------
# منطق كشف الألوان (كاتشب/ثوم) داخل الفيديو المرفوع
# ---------------------------------------------------------------------
def detect_frame_colors(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = (((h <= RED_RANGE["hue_max_low"]) | (h >= RED_RANGE["hue_min_high"])) &
           (s > RED_RANGE["sat_min"]) & (v > RED_RANGE["val_min"]))
    white = ((s < WHITE_RANGE["sat_max"]) & (v > WHITE_RANGE["val_min"]))
    return int(red.sum()), int(white.sum())


def analyze_video_file(path, roi=None, max_duration_seconds=360, frame_skip=2):
    """
    يمر على الفيديو ويرجع أعلى قيمة أحمر/أبيض شوهدت.
    يعالج إطاراً كل (frame_skip) إطارات لتوفير الوقت على مقاطع أطول (حتى 6 دقائق افتراضياً)،
    بدل معالجة كل إطار على حدة - يكفي تماماً لكشف نشاط رش الصوص لأنه يستمر عدة ثوانٍ متتالية.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("تعذّر فتح ملف الفيديو")

    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    max_raw_frames = int(fps * max_duration_seconds)

    max_red, max_white, frame_count, analyzed_count = 0, 0, 0, 0
    while True:
        ret, frame = cap.read()
        if not ret or frame_count >= max_raw_frames:
            break
        frame_count += 1
        if frame_count % frame_skip != 0:
            continue

        analyzed_count += 1
        analysis_frame = frame
        if roi is not None:
            y0, y1, x0, x1 = roi
            analysis_frame = frame[y0:y1, x0:x1]
        r, w = detect_frame_colors(analysis_frame)
        max_red, max_white = max(max_red, r), max(max_white, w)

    cap.release()
    return max_red, max_white, analyzed_count


# ---------------------------------------------------------------------
# الواجهة (تعرض صفحة اللوحة)
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------
# API: تسجيل نتيجة تحقق جديدة (يدوي، أو من سكربت خارجي)
# ---------------------------------------------------------------------
@app.route("/api/verify", methods=["POST"])
def api_verify():
    data = request.get_json(force=True)
    order_id, result, missing, extra = record_verification(
        data.get("employee"), data.get("required", []), data.get("detected", []), data.get("order_number"),
    )
    return jsonify({"order_id": order_id, "result": result, "missing": missing, "extra": extra})


# ---------------------------------------------------------------------
# API: رفع فيديو، تحليله، وحفظه بشكل دائم بمجلد uploaded_videos
# ---------------------------------------------------------------------
@app.route("/api/analyze_video", methods=["POST"])
def api_analyze_video():
    video_file = request.files.get("video")
    employee = request.form.get("employee", "").strip()
    required_raw = request.form.get("required", "")
    order_number = request.form.get("order_number") or None

    if not video_file or not employee or not required_raw:
        return jsonify({"error": "يلزم اختيار فيديو، وإدخال اسم الموظف والصوصات المطلوبة"}), 400

    required = [s.strip() for s in required_raw.split(",") if s.strip()]

    # نحفظ الفيديو بشكل دائم بمجلد uploaded_videos (لاستخدامه لاحقاً بتدريب الموديل)
    # تنبيه: تخزين السيرفر المجاني غير دائم (Ephemeral) - يُنصح بتحميل الفيديوهات
    # دورياً عبر /api/videos قبل أي إعادة نشر (Redeploy) لتفادي فقدانها.
    original_name = video_file.filename or "video.mp4"
    suffix = os.path.splitext(original_name)[1] or ".mp4"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stored_filename = f"{timestamp}_{random.randint(1000,9999)}{suffix}"
    stored_path = os.path.join(UPLOAD_DIR, stored_filename)

    try:
        video_file.save(stored_path)
        file_size = os.path.getsize(stored_path)

        max_red, max_white, frames_analyzed = analyze_video_file(stored_path)

        detected = []
        if "كاتشب" in required and max_red >= RED_DETECTION_THRESHOLD:
            detected.append("كاتشب")
        if "ثوم" in required and max_white >= WHITE_DETECTION_THRESHOLD:
            detected.append("ثوم")

        order_id, result, missing, extra = record_verification(employee, required, detected, order_number)

        conn = get_db()
        conn.execute(
            """INSERT INTO video_uploads
               (stored_filename, original_filename, employee, required_sauces, detected_sauces,
                result, frames_analyzed, max_red, max_white, file_size_bytes, uploaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stored_filename, original_name, employee, json.dumps(required, ensure_ascii=False),
             json.dumps(detected, ensure_ascii=False), result, frames_analyzed, max_red, max_white,
             file_size, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "order_id": order_id, "result": result, "missing": missing, "extra": extra,
            "detected": detected, "frames_analyzed": frames_analyzed,
            "max_red": max_red, "max_white": max_white,
            "video_saved_as": stored_filename,
        })
    except Exception as e:
        # لو صار خطأ أثناء التحليل، نحذف الملف المحفوظ لتفادي تراكم ملفات تالفة بلا فائدة
        if os.path.exists(stored_path):
            os.remove(stored_path)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------
# API: عرض كل الفيديوهات المحفوظة (للمراجعة لاحقاً أو تدريب الموديل)
# ---------------------------------------------------------------------
@app.route("/api/videos")
def api_list_videos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM video_uploads ORDER BY video_id DESC").fetchall()
    conn.close()
    videos = [{
        "video_id": r["video_id"], "original_filename": r["original_filename"],
        "employee": r["employee"], "required_sauces": json.loads(r["required_sauces"] or "[]"),
        "detected_sauces": json.loads(r["detected_sauces"] or "[]"), "result": r["result"],
        "frames_analyzed": r["frames_analyzed"], "file_size_mb": round((r["file_size_bytes"] or 0) / (1024*1024), 2),
        "uploaded_at": r["uploaded_at"],
    } for r in rows]
    return jsonify({"videos": videos, "note": "التخزين مؤقت على السيرفر المجاني - يُنصح بتحميل الفيديوهات دورياً"})


# ---------------------------------------------------------------------
# API: تحميل فيديو محفوظ بعينه (لأرشفته أو استخدامه بتدريب الموديل)
# ---------------------------------------------------------------------
@app.route("/api/videos/<int:video_id>/download")
def api_download_video(video_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM video_uploads WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()

    if row is None:
        return jsonify({"error": "الفيديو غير موجود"}), 404

    path = os.path.join(UPLOAD_DIR, row["stored_filename"])
    if not os.path.exists(path):
        return jsonify({"error": "الملف لم يعد موجوداً على السيرفر (ربما تم فقدانه بعد إعادة نشر)"}), 404

    return send_file(path, as_attachment=True, download_name=row["original_filename"])


# ---------------------------------------------------------------------
# API: بيانات لوحة التحليلات (حقيقية من قاعدة البيانات، مو أرقام ثابتة)
# ---------------------------------------------------------------------
@app.route("/api/dashboard")
def api_dashboard():
    conn = get_db()
    cur = conn.cursor()

    total_orders = cur.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
    correct = cur.execute("SELECT COUNT(*) FROM verifications WHERE result='match'").fetchone()[0]
    accuracy = round(100 * correct / total_orders, 1) if total_orders else None
    open_alerts = cur.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]

    # دقة كل موظف
    emp_rows = cur.execute("""
        SELECT e.name,
               COUNT(*) AS total,
               SUM(CASE WHEN v.result='match' THEN 1 ELSE 0 END) AS correct
        FROM verifications v
        JOIN orders o ON o.order_id = v.order_id
        JOIN employees e ON e.employee_id = o.employee_id
        GROUP BY e.employee_id
    """).fetchall()
    employee_accuracy = [
        {"name": r["name"], "accuracy": round(100 * r["correct"] / r["total"], 1), "total": r["total"]}
        for r in emp_rows
    ]

    # أكثر الأصناف نسياناً
    miss_rows = cur.execute("SELECT missing_items FROM verifications WHERE result='mismatch'").fetchall()
    miss_count = {}
    for r in miss_rows:
        for item in json.loads(r["missing_items"] or "[]"):
            miss_count[item] = miss_count.get(item, 0) + 1
    most_missed = sorted(miss_count.items(), key=lambda x: -x[1])

    # آخر التنبيهات
    alert_rows = cur.execute("""
        SELECT a.alert_id, a.status, o.pos_order_number, e.name AS employee, v.missing_items
        FROM alerts a
        JOIN verifications v ON v.verification_id = a.verification_id
        JOIN orders o ON o.order_id = v.order_id
        JOIN employees e ON e.employee_id = o.employee_id
        ORDER BY a.alert_id DESC LIMIT 10
    """).fetchall()
    alerts = [
        {"order_number": r["pos_order_number"], "employee": r["employee"],
         "missing": json.loads(r["missing_items"] or "[]"), "status": r["status"]}
        for r in alert_rows
    ]

    conn.close()
    return jsonify({
        "total_orders": total_orders,
        "accuracy": accuracy,
        "open_alerts": open_alerts,
        "employee_accuracy": employee_accuracy,
        "most_missed": most_missed,
        "alerts": alerts,
    })


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
else:
    # عند التشغيل عبر gunicorn (وقت النشر الفعلي)، __name__ لا يساوي "__main__"
    # لازم نهيّئ قاعدة البيانات هنا كمان، وإلا الجداول ما تُنشأ إطلاقاً
    init_db()


@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "حجم الفيديو أكبر من الحد المسموح (150 ميجا). جرّب مقطعاً أقصر أو بجودة أقل."}), 413
