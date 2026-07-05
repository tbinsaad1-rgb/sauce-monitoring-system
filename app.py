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
from flask import Flask, jsonify, request, render_template
import sqlite3
import os
import random
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "sauce_monitor.db")


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


import json


def compute_result(required, detected):
    req, det = set(required), set(detected)
    missing = sorted(req - det)
    extra = sorted(det - req)
    result = "match" if not missing and not extra else "mismatch"
    return result, missing, extra


# ---------------------------------------------------------------------
# الواجهة (تعرض صفحة اللوحة)
# ---------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------
# API: تسجيل نتيجة تحقق جديدة (هذا اللي "يستدعيه" كود الكشف الحقيقي لاحقاً)
# ---------------------------------------------------------------------
@app.route("/api/verify", methods=["POST"])
def api_verify():
    data = request.get_json(force=True)
    employee_name = data.get("employee")
    required = data.get("required", [])
    detected = data.get("detected", [])
    pos_order_number = data.get("order_number", f"AUTO-{random.randint(1000,9999)}")

    conn = get_db()
    cur = conn.cursor()

    emp_row = cur.execute("SELECT employee_id FROM employees WHERE name = ?", (employee_name,)).fetchone()
    if emp_row is None:
        cur.execute("INSERT INTO employees (name) VALUES (?)", (employee_name,))
        employee_id = cur.lastrowid
    else:
        employee_id = emp_row["employee_id"]

    now = datetime.now().isoformat()
    cur.execute(
        "INSERT INTO orders (pos_order_number, required_sauces, employee_id, created_at) VALUES (?, ?, ?, ?)",
        (pos_order_number, json.dumps(required, ensure_ascii=False), employee_id, now),
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

    return jsonify({"order_id": order_id, "result": result, "missing": missing, "extra": extra})


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
