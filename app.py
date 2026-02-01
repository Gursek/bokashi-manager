from flask import Flask, render_template, jsonify, request, redirect, url_for, send_file
from flask_wtf import FlaskForm
from wtforms import SubmitField, StringField, FloatField, TextAreaField
from wtforms.validators import DataRequired
import datetime
import csv
import os
import threading
import time
import json
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = "secret"

# ------------------ DATA STORAGE ------------------
sensor_data = {
    "temperature": 30.0,
    "humidity": 65.0,
    "ammonia": 0.2,
    "last_updated": None
}

# Compost batches storage
BATCH_FILE = "batches.json"
ALERT_FILE = "alerts.json"
LOG_FILE = "sensor_log.csv"
SETTINGS_FILE = "settings.json"

# Initialize batch data
batches = []
alerts = []

# Device control state (sensors and GSM only; no fan/heater on this device)
device_state = {
    "sensors_online": True,
    "gsm_status": "standby"
}

# Settings (loaded from file)
settings = {
    "thresholds": {"max_temp": 35, "min_temp": 15, "max_humidity": 70, "max_ammonia": 5},
    "notifications": {"email_enabled": True, "sms_enabled": False, "push_enabled": True, "email": "", "phone": ""},
    "logging": {"interval_seconds": 60, "auto_backup": True}
}

# System info (can be extended with real platform data)
app_start_time = time.time()

# Images storage (captures / snapshots)
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "captures")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# ------------------ HELPER FUNCTIONS ------------------
def load_batches():
    global batches
    if os.path.exists(BATCH_FILE):
        with open(BATCH_FILE, 'r') as f:
            batches = json.load(f)
    else:
        batches = []

def save_batches():
    with open(BATCH_FILE, 'w') as f:
        json.dump(batches, f, indent=2)

def load_alerts():
    global alerts
    if os.path.exists(ALERT_FILE):
        with open(ALERT_FILE, 'r') as f:
            alerts = json.load(f)
    else:
        alerts = []

def save_alerts():
    with open(ALERT_FILE, 'w') as f:
        json.dump(alerts, f, indent=2)

def add_alert(level, message):
    new_id = max([a["id"] for a in alerts], default=0) + 1
    alert = {
        "id": new_id,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,  # info, warning, danger
        "message": message,
        "read": False
    }
    alerts.insert(0, alert)
    save_alerts()

def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            loaded = json.load(f)
            settings["thresholds"] = loaded.get("thresholds", settings["thresholds"])
            settings["notifications"] = loaded.get("notifications", settings["notifications"])
            settings["logging"] = loaded.get("logging", settings["logging"])

def save_settings():
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

# ------------------ CSV INITIALIZATION ------------------
def initialize_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode="w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "Timestamp",
                "Temperature",
                "Humidity",
                "Ammonia"
            ])

# ------------------ SENSOR SIMULATION ------------------
def read_sensors():
    sensor_data["temperature"] += 0.1
    sensor_data["humidity"] -= 0.1
    sensor_data["ammonia"] += 0.01
    sensor_data["last_updated"] = datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    
    # Check for alerts using saved thresholds
    th = settings.get("thresholds", {})
    max_temp = th.get("max_temp", 35)
    max_ammonia = th.get("max_ammonia", 5)
    if sensor_data["temperature"] > max_temp:
        add_alert("warning", f"High temperature detected: {sensor_data['temperature']:.1f}°C")
    if sensor_data["ammonia"] > max_ammonia:
        add_alert("danger", f"High ammonia levels: {sensor_data['ammonia']:.2f} ppm")

def log_sensor_data():
    with open(LOG_FILE, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            sensor_data["last_updated"],
            round(sensor_data["temperature"], 2),
            round(sensor_data["humidity"], 2),
            round(sensor_data["ammonia"], 3)
        ])

# ------------------ BACKGROUND TASK ------------------
def background_task():
    initialize_csv()
    load_batches()
    load_alerts()
    load_settings()
    while True:
        load_settings()
        read_sensors()
        log_sensor_data()
        interval = settings.get("logging", {}).get("interval_seconds", 60)
        time.sleep(interval)

# ------------------ FLASK ROUTES ------------------
@app.route("/")
def dashboard():
    return render_template("dashboard.html", active_page="dashboard")

@app.route("/batches")
def batch_management():
    load_batches()
    return render_template("batches.html", batches=batches, active_page="batches")

@app.route("/batches/add", methods=["POST"])
def add_batch():
    data = request.form
    batch = {
        "id": len(batches) + 1,
        "name": data.get("name"),
        "start_date": data.get("start_date"),
        "status": "active",
        "weight": float(data.get("weight", 0)),
        "notes": data.get("notes", "")
    }
    batches.append(batch)
    save_batches()
    add_alert("info", f"New batch '{batch['name']}' created")
    return redirect(url_for('batch_management'))

@app.route("/batches/complete/<int:batch_id>")
def complete_batch(batch_id):
    for batch in batches:
        if batch["id"] == batch_id:
            batch["status"] = "completed"
            batch["end_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    save_batches()
    add_alert("info", "Batch marked as completed")
    return redirect(url_for('batch_management'))

@app.route("/logs-page")
def logs_page():
    return render_template("logs.html", active_page="logs")

@app.route("/control")
def device_control():
    return render_template("control.html", active_page="control")


def get_capture_images():
    """Return list of image filenames in captures folder (newest first by mtime)."""
    if not os.path.isdir(IMAGES_DIR):
        return []
    files = []
    for name in os.listdir(IMAGES_DIR):
        if name.lower().split(".")[-1] in ALLOWED_IMAGE_EXTENSIONS:
            path = os.path.join(IMAGES_DIR, name)
            if os.path.isfile(path):
                files.append((name, os.path.getmtime(path)))
    files.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in files]


@app.route("/control/images")
def view_images():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    images = get_capture_images()
    return render_template("images.html", images=images, active_page="control")


@app.route("/control/images/upload", methods=["POST"])
def upload_image():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    if "image" not in request.files and "file" not in request.files:
        return redirect(url_for("view_images"))
    file = request.files.get("image") or request.files.get("file")
    if not file or file.filename == "":
        return redirect(url_for("view_images"))
    ext = file.filename.lower().split(".")[-1]
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        add_alert("warning", "Invalid image type. Use PNG, JPG, GIF or WebP.")
        return redirect(url_for("view_images"))
    safe_name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "." + ext
    file.save(os.path.join(IMAGES_DIR, safe_name))
    add_alert("info", f"Image saved: {safe_name}")
    return redirect(url_for("view_images"))


@app.route("/control/add-bran", methods=["POST"])
def add_bran():
    amount = request.form.get("amount")
    add_alert("info", f"Added {amount}g of Bokashi bran")
    return redirect(url_for('device_control'))

@app.route("/alerts-page")
def alerts_page():
    load_alerts()
    return render_template("alerts.html", alerts=alerts, active_page="alerts")

@app.route("/alerts/mark-read/<int:alert_id>")
def mark_alert_read(alert_id):
    for alert in alerts:
        if alert["id"] == alert_id:
            alert["read"] = True
    save_alerts()
    return redirect(url_for('alerts_page'))

@app.route("/settings", methods=["GET", "POST"], endpoint="settings")
def settings_page():
    load_settings()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "thresholds":
            settings["thresholds"] = {
                "max_temp": float(request.form.get("max_temp", 35)),
                "min_temp": float(request.form.get("min_temp", 15)),
                "max_humidity": float(request.form.get("max_humidity", 70)),
                "max_ammonia": float(request.form.get("max_ammonia", 5))
            }
        elif section == "notifications":
            settings["notifications"] = {
                "email_enabled": request.form.get("email_enabled") == "on",
                "sms_enabled": request.form.get("sms_enabled") == "on",
                "push_enabled": request.form.get("push_enabled") == "on",
                "email": request.form.get("email", ""),
                "phone": request.form.get("phone", "")
            }
        elif section == "logging":
            settings["logging"] = {
                "interval_seconds": int(request.form.get("interval_seconds", 60)),
                "auto_backup": request.form.get("auto_backup") == "on"
            }
        save_settings()
        add_alert("info", "Settings saved successfully")
        return redirect(url_for("settings"))
    uptime_seconds = int(time.time() - app_start_time)
    uptime_str = f"{uptime_seconds // 3600} hours {(uptime_seconds % 3600) // 60} minutes"
    system_info = {
        "device_name": "Bokashi Composter v1.0",
        "version": "1.0.0",
        "raspberry_model": "Raspberry Pi 4",
        "uptime": uptime_str,
        "cpu_temp": "45°C",
        "storage_used": "2.3 GB / 32 GB"
    }
    return render_template("settings.html", active_page="settings", settings=settings, system_info=system_info)


@app.route("/api/device-status")
def api_device_status():
    return jsonify({
        "sensors": {
            "temperature": "online" if device_state["sensors_online"] else "offline",
            "humidity": "online" if device_state["sensors_online"] else "offline",
            "air_quality": "online" if device_state["sensors_online"] else "offline"
        },
        "gsm_status": device_state["gsm_status"]
    })


@app.route("/control/test-alert", methods=["POST"])
def test_alert():
    add_alert("warning", "This is a test alert from Device Control.")
    return redirect(url_for("device_control"))


@app.route("/logs/export")
def export_logs_csv():
    if not os.path.exists(LOG_FILE):
        return "No logs yet", 404
    with open(LOG_FILE, "r") as f:
        content = f.read()
    buffer = io.BytesIO(content.encode("utf-8"))
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"bokashi_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    )


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    initialize_csv()  # overwrites with header only
    add_alert("info", "Sensor logs cleared")
    return redirect(url_for("logs_page"))

# ------------------ API ROUTES ------------------
@app.route("/api/sensors")
def api_sensors():
    return jsonify(sensor_data)

@app.route("/logs")
def api_logs():
    logs = []
    with open(LOG_FILE, "r") as file:
        reader = csv.DictReader(file)
        for row in reader:
            logs.append(row)
    # Return newest first so the table shows most recent at top
    return jsonify(list(reversed(logs)))

@app.route("/api/stats")
def api_stats():
    load_batches()
    active_batches = len([b for b in batches if b["status"] == "active"])
    completed_batches = len([b for b in batches if b["status"] == "completed"])
    unread_alerts = len([a for a in alerts if not a["read"]])
    
    return jsonify({
        "active_batches": active_batches,
        "completed_batches": completed_batches,
        "unread_alerts": unread_alerts,
        "total_weight": sum([b.get("weight", 0) for b in batches])
    })

# ------------------ START APP ------------------
if __name__ == "__main__":
    threading.Thread(target=background_task, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)