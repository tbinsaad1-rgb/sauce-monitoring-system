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

# كتالوج الصوصات: كل صوص له لون ونطاق كشف ومستوى ثقة (انظر القسم 7 و17 بالمستند التقني)
# reliable=True: مؤكَّد بالاختبارات الحقيقية (كاتشب/ثوم) — reliable=False: تجريبي، دقته منخفضة
# لأن لونه قريب من لون الأكل الذهبي المقلي
SAUCE_CATALOG = {
    "كاتشب":  {"color_hex": "#C0302F", "reliable": True,
                "range": {"hue_max_low": 8, "hue_min_high": 172, "sat_min": 190, "val_min": 50},
                "threshold": 150},
    "ثوم":    {"color_hex": "#EDE7DA", "reliable": True,
                "range": {"sat_max": 45, "val_min": 150},
                "threshold": 3000},
    "كوكتيل": {"color_hex": "#E0952E", "reliable": False,
                "range": {"hue_min": 9, "hue_max": 20, "sat_min": 180, "val_min": 60},
                "threshold": 500},
    "بافلو":  {"color_hex": "#7A2A1E", "reliable": False,
                "range": {"hue_max_low": 14, "sat_min": 120, "val_min": 25, "val_max": 75},
                "threshold": 500},
}


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

    CREATE TABLE IF NOT EXISTS dish_corrections (
        correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id INTEGER NOT NULL,
        order_position INTEGER NOT NULL,
        system_detected TEXT NOT NULL,      -- JSON: شنو قاله النظام (detected_all)
        ground_truth TEXT NOT NULL,         -- JSON: شنو صحّحه الإنسان (الحقيقة الفعلية)
        raw_pixel_counts TEXT,              -- JSON: القياسات الخام لكل صوص - تُستخدم كميزات (Features) لتدريب موديل لاحقاً
        was_fully_correct INTEGER NOT NULL, -- 1 لو تطابق النظام مع الحقيقة تماماً، 0 لو فيه أي فرق
        missed_by_system TEXT,              -- JSON: صوصات موجودة فعلياً لكن النظام فاتته
        wrongly_detected TEXT,              -- JSON: صوصات قالها النظام لكنها غير موجودة فعلياً
        corrected_at TEXT NOT NULL,
        FOREIGN KEY(video_id) REFERENCES video_uploads(video_id)
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

    # ترقية آمنة لقاعدة بيانات منشورة مسبقاً: نضيف الأعمدة الجديدة لو ما كانت موجودة
    # (ALTER TABLE ADD COLUMN تفشل لو العمود موجود أصلاً، فنتجاهل الخطأ حينها فقط)
    for alter_sql in [
        "ALTER TABLE video_uploads ADD COLUMN dish_results TEXT",
        "ALTER TABLE video_uploads ADD COLUMN num_dishes INTEGER DEFAULT 1",
    ]:
        try:
            cur.execute(alter_sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # العمود موجود مسبقاً، لا حاجة لأي إجراء

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
# منطق كشف الألوان - لكل صوصات الكتالوج معاً بضربة وحدة لكل إطار
# ---------------------------------------------------------------------
def detect_sauce_pixels(hsv_frame, sauce_name):
    rng = SAUCE_CATALOG[sauce_name]["range"]
    h, s, v = hsv_frame[:, :, 0], hsv_frame[:, :, 1], hsv_frame[:, :, 2]

    if "hue_min_high" in rng:  # نطاق يلف حول الصفر (أحمر) - مثل الكاتشب
        mask = (((h <= rng["hue_max_low"]) | (h >= rng["hue_min_high"])) &
                (s > rng["sat_min"]) & (v > rng["val_min"]))
    elif "hue_min" in rng:  # نطاق هيو عادي محصور بين قيمتين - مثل الكوكتيل
        mask = ((h >= rng["hue_min"]) & (h <= rng["hue_max"]) &
                (s > rng["sat_min"]) & (v > rng["val_min"]))
    elif "val_max" in rng:  # نطاق أحمر غامق محصور بقيمة سطوع - مثل البافلو
        mask = ((h <= rng["hue_max_low"]) & (s > rng["sat_min"]) &
                (v > rng["val_min"]) & (v < rng["val_max"]))
    else:  # نطاق تشبع منخفض وسطوع عالٍ - مثل الثوم (أبيض)
        mask = ((s < rng["sat_max"]) & (v > rng["val_min"]))

    return int(mask.sum())


def analyze_video_multi_dish(path, num_dishes=1, roi=None, max_duration_seconds=360, frame_skip=2):
    """
    يحلل الفيديو ويرجع أعلى قيمة بكسل لكل صوص بكل "طبق" (شريحة عمودية من الإطار).
    الأطباق تُرقَّم من اليمين لليسار (طبق 1 = أقصى اليمين)، مطابقةً لترتيب وضع
    الفيش/الصحون على الطاولة كما لوحظ بالصور الحقيقية (القسم 17 بالمستند التقني).

    ملاحظة مهمة: تقسيم الإطار لعدة شرائح متساوية افتراض مبسّط (كل طبق بنفس
    العرض تقريباً)؛ قد يحتاج تعديلاً لاحقاً حسب الترتيب الفعلي بالفيديو.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("تعذّر فتح ملف الفيديو")

    fps = cap.get(cv2.CAP_PROP_FPS) or 10.0
    max_raw_frames = int(fps * max_duration_seconds)

    # نتيجة لكل طبق: قاموس {اسم_الصوص: أعلى قيمة بكسل شوهدت}
    dish_max = [{name: 0 for name in SAUCE_CATALOG} for _ in range(num_dishes)]
    frame_count, analyzed_count = 0, 0

    while True:
        ret, frame = cap.read()
        if not ret or frame_count >= max_raw_frames:
            break
        frame_count += 1
        if frame_count % frame_skip != 0:
            continue
        analyzed_count += 1

        work_frame = frame
        if roi is not None:
            y0, y1, x0, x1 = roi
            work_frame = frame[y0:y1, x0:x1]

        width = work_frame.shape[1]
        slice_width = width // num_dishes

        for dish_idx in range(num_dishes):
            x_start = dish_idx * slice_width
            x_end = width if dish_idx == num_dishes - 1 else (dish_idx + 1) * slice_width
            slice_frame = work_frame[:, x_start:x_end]
            hsv_slice = cv2.cvtColor(slice_frame, cv2.COLOR_BGR2HSV)

            for sauce_name in SAUCE_CATALOG:
                count = detect_sauce_pixels(hsv_slice, sauce_name)
                if count > dish_max[dish_idx][sauce_name]:
                    dish_max[dish_idx][sauce_name] = count

    cap.release()

    # ترقيم الأطباق من اليمين لليسار: الشريحة الأخيرة بالإحداثيات (أقصى يمين الصورة) = طبق رقم 1
    dish_max_right_to_left = list(reversed(dish_max))
    return dish_max_right_to_left, analyzed_count


# ---------------------------------------------------------------------
# الواجهة (تعرض صفحة اللوحة)
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------
# API: كتالوج الصوصات (الاسم، اللون، مستوى الثقة) - تستخدمه الواجهة لعرض القائمة والألوان
# ---------------------------------------------------------------------
@app.route("/api/catalog")
def api_catalog():
    return jsonify({
        name: {"color_hex": info["color_hex"], "reliable": info["reliable"]}
        for name, info in SAUCE_CATALOG.items()
    })


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
    order_number_base = request.form.get("order_number") or None
    try:
        num_dishes = max(1, min(8, int(request.form.get("num_dishes", 1))))
    except (TypeError, ValueError):
        num_dishes = 1

    if not video_file or not employee:
        return jsonify({"error": "يلزم اختيار فيديو وإدخال اسم الموظف"}), 400

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

        dish_max_counts, frames_analyzed = analyze_video_multi_dish(stored_path, num_dishes=num_dishes)

        # عتبة الكشف تُقسَّم تقريبياً على عدد الأطباق (كل طبق ياخذ حصة أصغر من مساحة الإطار)
        dishes_report = []
        for i, counts in enumerate(dish_max_counts, start=1):
            detected_all = []       # كل اللي انكشف (بغض النظر عن المطلوب) - للتحقق من صحة البرنامج
            detected_reliable = []  # المؤكَّد بس (كاتشب/ثوم)
            for sauce_name, info in SAUCE_CATALOG.items():
                scaled_threshold = max(info["threshold"] / num_dishes, info["threshold"] * 0.15)
                if counts[sauce_name] >= scaled_threshold:
                    detected_all.append(sauce_name)
                    if info["reliable"]:
                        detected_reliable.append(sauce_name)

            result, missing, extra = compute_result(required, detected_reliable) if required else (None, [], [])
            order_number = f"{order_number_base or 'AUTO'}-DISH{i}" if num_dishes > 1 else order_number_base

            if required:
                record_verification(employee, required, detected_reliable, order_number)

            dishes_report.append({
                "order_position": i,
                "detected_all": detected_all,
                "detected_reliable": detected_reliable,
                "raw_pixel_counts": counts,
                "result": result,
                "missing": missing,
            })

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO video_uploads
               (stored_filename, original_filename, employee, required_sauces, detected_sauces,
                result, frames_analyzed, max_red, max_white, file_size_bytes, uploaded_at,
                dish_results, num_dishes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stored_filename, original_name, employee, json.dumps(required, ensure_ascii=False),
             json.dumps(dishes_report[0]["detected_all"], ensure_ascii=False),
             dishes_report[0]["result"], frames_analyzed,
             dishes_report[0]["raw_pixel_counts"].get("كاتشب", 0),
             dishes_report[0]["raw_pixel_counts"].get("ثوم", 0),
             file_size, datetime.now().isoformat(),
             json.dumps(dishes_report, ensure_ascii=False), num_dishes),
        )
        video_id = cur.lastrowid
        conn.commit()
        conn.close()

        return jsonify({
            "video_id": video_id,
            "frames_analyzed": frames_analyzed,
            "num_dishes": num_dishes,
            "dishes": dishes_report,
            "video_saved_as": stored_filename,
        })
    except Exception as e:
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
    videos = []
    for r in rows:
        try:
            dish_results = json.loads(r["dish_results"]) if r["dish_results"] else None
        except (KeyError, TypeError, json.JSONDecodeError):
            dish_results = None
        videos.append({
            "video_id": r["video_id"], "original_filename": r["original_filename"],
            "employee": r["employee"], "required_sauces": json.loads(r["required_sauces"] or "[]"),
            "num_dishes": r["num_dishes"] if "num_dishes" in r.keys() else 1,
            "dish_results": dish_results,
            "frames_analyzed": r["frames_analyzed"], "file_size_mb": round((r["file_size_bytes"] or 0) / (1024*1024), 2),
            "uploaded_at": r["uploaded_at"],
        })
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
# API: تسجيل تصحيح بشري لنتيجة طبق معيّن (Ground Truth) - هذا ما يبني بيانات
# التدريب الحقيقية لموديل الذكاء الاصطناعي المستقبلي، بدل الاعتماد على عتبات ثابتة
# ---------------------------------------------------------------------
@app.route("/api/correct_dish", methods=["POST"])
def api_correct_dish():
    data = request.get_json(force=True)
    video_id = data.get("video_id")
    order_position = data.get("order_position")
    ground_truth = data.get("ground_truth", [])

    if video_id is None or order_position is None:
        return jsonify({"error": "يلزم تحديد video_id وorder_position"}), 400

    conn = get_db()
    row = conn.execute("SELECT dish_results FROM video_uploads WHERE video_id = ?", (video_id,)).fetchone()
    if row is None or not row["dish_results"]:
        conn.close()
        return jsonify({"error": "الفيديو أو نتائج الأطباق غير موجودة"}), 404

    dishes = json.loads(row["dish_results"])
    dish = next((d for d in dishes if d["order_position"] == order_position), None)
    if dish is None:
        conn.close()
        return jsonify({"error": f"لا يوجد طبق برقم {order_position} بهذا الفيديو"}), 404

    system_detected = dish["detected_all"]
    raw_counts = dish["raw_pixel_counts"]

    missed_by_system = sorted(set(ground_truth) - set(system_detected))   # موجود فعلياً لكن النظام ما شافه
    wrongly_detected = sorted(set(system_detected) - set(ground_truth))   # النظام قاله بس مو صحيح
    was_fully_correct = 1 if not missed_by_system and not wrongly_detected else 0

    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO dish_corrections
           (video_id, order_position, system_detected, ground_truth, raw_pixel_counts,
            was_fully_correct, missed_by_system, wrongly_detected, corrected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, order_position, json.dumps(system_detected, ensure_ascii=False),
         json.dumps(ground_truth, ensure_ascii=False), json.dumps(raw_counts, ensure_ascii=False),
         was_fully_correct, json.dumps(missed_by_system, ensure_ascii=False),
         json.dumps(wrongly_detected, ensure_ascii=False), now),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "was_fully_correct": bool(was_fully_correct),
        "missed_by_system": missed_by_system,
        "wrongly_detected": wrongly_detected,
    })


# ---------------------------------------------------------------------
# API: عرض كل التصحيحات المسجَّلة + إحصائية دقة النظام الفعلية مقارنة بالحقيقة
# ---------------------------------------------------------------------
@app.route("/api/corrections")
def api_list_corrections():
    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, v.original_filename
        FROM dish_corrections c
        JOIN video_uploads v ON v.video_id = c.video_id
        ORDER BY c.correction_id DESC
    """).fetchall()
    conn.close()

    corrections = [{
        "correction_id": r["correction_id"], "video_id": r["video_id"],
        "original_filename": r["original_filename"], "order_position": r["order_position"],
        "system_detected": json.loads(r["system_detected"]), "ground_truth": json.loads(r["ground_truth"]),
        "was_fully_correct": bool(r["was_fully_correct"]),
        "missed_by_system": json.loads(r["missed_by_system"] or "[]"),
        "wrongly_detected": json.loads(r["wrongly_detected"] or "[]"),
        "corrected_at": r["corrected_at"],
    } for r in rows]

    total = len(corrections)
    fully_correct = sum(1 for c in corrections if c["was_fully_correct"])
    accuracy = round(100 * fully_correct / total, 1) if total else None

    return jsonify({"corrections": corrections, "total": total, "system_accuracy_vs_ground_truth": accuracy})


# ---------------------------------------------------------------------
# API: تصدير بيانات التصحيح كملف CSV - جاهز لاستخدامه بتدريب موديل لاحقاً
# (كل صف = طبق واحد، بميزاته الخام "raw_pixel_counts" وتصنيفه الحقيقي "ground_truth")
# ---------------------------------------------------------------------
@app.route("/api/corrections/export")
def api_export_corrections():
    import csv
    import io

    conn = get_db()
    rows = conn.execute("""
        SELECT c.*, v.original_filename
        FROM dish_corrections c
        JOIN video_uploads v ON v.video_id = c.video_id
        ORDER BY c.correction_id
    """).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    sauce_names = list(SAUCE_CATALOG.keys())
    writer.writerow(
        ["correction_id", "video_filename", "order_position"] +
        [f"raw_pixels_{s}" for s in sauce_names] +
        ["system_detected", "ground_truth", "was_fully_correct", "missed_by_system", "wrongly_detected", "corrected_at"]
    )
    for r in rows:
        raw_counts = json.loads(r["raw_pixel_counts"] or "{}")
        writer.writerow(
            [r["correction_id"], r["original_filename"], r["order_position"]] +
            [raw_counts.get(s, "") for s in sauce_names] +
            [json.loads(r["system_detected"]), json.loads(r["ground_truth"]), r["was_fully_correct"],
             json.loads(r["missed_by_system"] or "[]"), json.loads(r["wrongly_detected"] or "[]"), r["corrected_at"]]
        )

    csv_data = output.getvalue()
    return app.response_class(
        csv_data, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=training_data_corrections.csv"},
    )


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
