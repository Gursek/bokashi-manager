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
import gspread
from google.oauth2.service_account import Credentials

# ------------------ SENSOR IMPORTS ------------------
try:
    import Adafruit_DHT
    DHT_AVAILABLE = True
except ImportError:
    DHT_AVAILABLE = False
    print("[WARN] Adafruit_DHT not found. DHT22 readings will be simulated.")

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    print("[WARN] RPi.GPIO not found. Ultrasonic & water sensor readings will be simulated.")

# ------------------ GPIO PIN CONFIGURATION ------------------
DHT_SENSOR_TYPE  = Adafruit_DHT.DHT22 if DHT_AVAILABLE else None
DHT_PIN          = 4    # GPIO4  – Pin 7
TRIG_PIN         = 23   # GPIO23 – Pin 16
ECHO_PIN         = 24   # GPIO24 – Pin 18  (via 1kΩ/2kΩ voltage divider)
WATER_PIN        = 17   # GPIO17 – Pin 11  (via 1kΩ/2kΩ voltage divider)

# HC-SR04 calibration
# BIN_HEIGHT_CM: distance (cm) from the sensor to the bottom of the bin when empty.
# The sensor is mounted at the top looking down.
# When distance == BIN_HEIGHT_CM  → bin is   0% full (empty)
# When distance == 0              → bin is 100% full  (full)
BIN_HEIGHT_CM    = 40.0   # ← adjust to your actual bin interior height in cm
MAX_DISTANCE_CM  = 400.0  # sensor's max reliable range

# ------------------ GPIO SETUP ------------------
def setup_gpio():
    if not GPIO_AVAILABLE:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(TRIG_PIN,  GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(ECHO_PIN,  GPIO.IN)
    GPIO.setup(WATER_PIN, GPIO.IN)

app = Flask(__name__)
app.config['SECRET_KEY'] = "secret"

# ------------------ DATA STORAGE ------------------
sensor_data = {
    "temperature":   None,
    "humidity":      None,
    "bin_capacity":  None,   # percentage 0–100 (from HC-SR04 distance)
    "water_status":  None,   # "wet" / "dry"  (from HW-038)
    "last_updated":  None,
    "sensor_error":  None    # last error message, if any
}

# Compost batches storage
BATCH_FILE    = "batches.json"
ALERT_FILE    = "alerts.json"
LOG_FILE      = "sensor_log.csv"
SETTINGS_FILE = "settings.json"

batches = []
alerts  = []

device_state = {
    "sensors_online": True,
    "gsm_status": "standby"
}

settings = {
    "thresholds": {
        "max_temp":         35,
        "min_temp":         15,
        "max_humidity":     70,
        "max_bin_capacity": 90   # alert when bin is more than 90% full
    },
    "notifications": {
        "email_enabled": True,
        "sms_enabled":   False,
        "push_enabled":  True,
        "email":         "",
        "phone":         ""
    },
    "logging": {
        "interval_seconds": 60,
        "auto_backup":      True
    }
}

app_start_time = time.time()

IMAGES_DIR             = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "captures")
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
        "id":        new_id,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level":     level,   # info | warning | danger
        "message":   message,
        "read":      False
    }
    alerts.insert(0, alert)
    save_alerts()

def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            loaded = json.load(f)
            settings["thresholds"]    = loaded.get("thresholds",    settings["thresholds"])
            settings["notifications"] = loaded.get("notifications", settings["notifications"])
            logging_loaded            = loaded.get("logging",       settings["logging"])
            # Always store interval_seconds as int so the Jinja `==` comparison
            # in the <select> works correctly after a JSON round-trip.
            logging_loaded["interval_seconds"] = int(
                logging_loaded.get("interval_seconds", 60)
            )
            settings["logging"] = logging_loaded

def save_settings():
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

# ------------------ CSV INITIALIZATION ------------------
def initialize_csv():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, mode="w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow([
                "Timestamp", "Temperature", "Humidity",
                "Bin_Capacity_Pct", "Water_Status"
            ])

# ------------------ REAL SENSOR READS ------------------

def read_dht22():
    """Return (temperature_C, humidity_pct) or (None, None) on failure."""
    if not DHT_AVAILABLE:
        # Simulation fallback
        import random
        return round(28.0 + random.uniform(-1, 1), 1), round(65.0 + random.uniform(-2, 2), 1)
    humidity, temperature = Adafruit_DHT.read_retry(DHT_SENSOR_TYPE, DHT_PIN, retries=5)
    return temperature, humidity


def read_ultrasonic_cm():
    """
    Trigger HC-SR04 and return measured distance in cm.
    Returns None on timeout / error.
    """
    if not GPIO_AVAILABLE:
        import random
        return round(random.uniform(5, BIN_HEIGHT_CM), 1)

    # Send 10 µs trigger pulse
    GPIO.output(TRIG_PIN, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(TRIG_PIN, GPIO.LOW)

    timeout = time.time() + 0.04   # 40 ms max wait

    # Wait for ECHO to go HIGH
    pulse_start = time.time()
    while GPIO.input(ECHO_PIN) == GPIO.LOW:
        pulse_start = time.time()
        if time.time() > timeout:
            return None

    # Wait for ECHO to go LOW
    pulse_end = time.time()
    while GPIO.input(ECHO_PIN) == GPIO.HIGH:
        pulse_end = time.time()
        if time.time() > timeout:
            return None

    duration = pulse_end - pulse_start
    distance_cm = (duration * 34300) / 2   # speed of sound 343 m/s
    if distance_cm > MAX_DISTANCE_CM:
        return None
    return round(distance_cm, 1)


def distance_to_bin_capacity_pct(distance_cm):
    """
    Convert HC-SR04 distance to a bin-capacity percentage.

    The sensor is mounted at the top of the bin looking down.
    - distance == BIN_HEIGHT_CM  →  bin is empty  (0% full)
    - distance == 0              →  bin is full   (100% full)

    Only BIN_HEIGHT_CM is needed; we don't use the bin's circumference
    or cross-section since we just want a fill-height percentage.
    """
    if distance_cm is None:
        return None
    capacity = ((BIN_HEIGHT_CM - distance_cm) / BIN_HEIGHT_CM) * 100
    return round(max(0.0, min(100.0, capacity)), 1)


def read_water_sensor():
    """
    Read HW-038 digital output.
    GPIO.HIGH (1) → wet/water detected
    GPIO.LOW  (0) → dry/no water
    """
    if not GPIO_AVAILABLE:
        return "wet"
    state = GPIO.input(WATER_PIN)
    return "wet" if state == GPIO.HIGH else "dry"


def read_sensors():
    """Read all sensors, update sensor_data, and raise alerts as needed."""
    errors = []

    # --- DHT22 ---
    temperature, humidity = read_dht22()
    if temperature is None or humidity is None:
        errors.append("DHT22 read failed")
        add_alert("warning", "DHT22 sensor read failed – check wiring on GPIO4.")
    else:
        sensor_data["temperature"] = round(temperature, 1)
        sensor_data["humidity"]    = round(humidity, 1)

    # --- HC-SR04 ultrasonic → bin capacity ---
    distance = read_ultrasonic_cm()
    capacity_pct = distance_to_bin_capacity_pct(distance)
    if capacity_pct is None:
        errors.append("HC-SR04 timeout")
        add_alert("warning", "Ultrasonic sensor timeout – check wiring on GPIO23/GPIO24.")
    else:
        sensor_data["bin_capacity"] = capacity_pct

    # --- HW-038 water presence sensor ---
    sensor_data["water_status"] = read_water_sensor()

    # --- Timestamp & error state ---
    sensor_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sensor_data["sensor_error"] = "; ".join(errors) if errors else None
    device_state["sensors_online"] = len(errors) == 0

    # --- Threshold alerts ---
    th = settings.get("thresholds", {})
    if sensor_data["temperature"] is not None:
        if sensor_data["temperature"] > th.get("max_temp", 35):
            add_alert("warning", f"High temperature: {sensor_data['temperature']}°C")
        if sensor_data["temperature"] < th.get("min_temp", 15):
            add_alert("warning", f"Low temperature: {sensor_data['temperature']}°C")
    if sensor_data["humidity"] is not None:
        if sensor_data["humidity"] > th.get("max_humidity", 70):
            add_alert("warning", f"High humidity: {sensor_data['humidity']}%")
    if sensor_data["bin_capacity"] is not None:
        if sensor_data["bin_capacity"] > th.get("max_bin_capacity", 90):
            add_alert("danger", f"Bin almost full: {sensor_data['bin_capacity']}%")
    if sensor_data["water_status"] == "dry":
        add_alert("info", "HW-038 reports dry – no water detected.")


def log_sensor_data():
    with open(LOG_FILE, mode="a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([
            sensor_data["last_updated"],
            sensor_data["temperature"],
            sensor_data["humidity"],
            sensor_data["bin_capacity"],
            sensor_data["water_status"]
        ])

# ------------------ BACKGROUND TASK ------------------
def background_task():
    setup_gpio()
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
        "id":         len(batches) + 1,
        "name":       data.get("name"),
        "start_date": data.get("start_date"),
        "status":     "active",
        "weight":     float(data.get("weight", 0)),
        "notes":      data.get("notes", "")
    }
    batches.append(batch)
    save_batches()
    add_alert("info", f"New batch '{batch['name']}' created")
    return redirect(url_for('batch_management'))

@app.route("/batches/complete/<int:batch_id>")
def complete_batch(batch_id):
    for batch in batches:
        if batch["id"] == batch_id:
            batch["status"]   = "completed"
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
                "max_temp":         float(request.form.get("max_temp",          35)),
                "min_temp":         float(request.form.get("min_temp",          15)),
                "max_humidity":     float(request.form.get("max_humidity",      70)),
                "max_bin_capacity": float(request.form.get("max_bin_capacity",  90))
            }
        elif section == "notifications":
            settings["notifications"] = {
                "email_enabled": request.form.get("email_enabled") == "on",
                "sms_enabled":   request.form.get("sms_enabled")   == "on",
                "push_enabled":  request.form.get("push_enabled")  == "on",
                "email":         request.form.get("email",  ""),
                "phone":         request.form.get("phone",  "")
            }
        elif section == "logging":
            settings["logging"] = {
                "interval_seconds": int(request.form.get("interval_seconds", 60)),
                "auto_backup":      request.form.get("auto_backup") == "on"
            }
        save_settings()
        add_alert("info", "Settings saved successfully")
        return redirect(url_for("settings"))

    uptime_seconds = int(time.time() - app_start_time)
    uptime_str = f"{uptime_seconds // 3600} hours {(uptime_seconds % 3600) // 60} minutes"
    system_info = {
        "device_name":    "Bokashi Composter v1.0",
        "version":        "1.0.0",
        "raspberry_model": "Raspberry Pi 4",
        "uptime":         uptime_str,
        "cpu_temp":       "45°C",
        "storage_used":   "2.3 GB / 32 GB"
    }
    return render_template("settings.html", active_page="settings",
                           settings=settings, system_info=system_info)


@app.route("/api/device-status")
def api_device_status():
    return jsonify({
        "sensors": {
            "temperature": "online" if device_state["sensors_online"] else "offline",
            "humidity":    "online" if device_state["sensors_online"] else "offline",
            "water":       "online" if device_state["sensors_online"] else "offline"
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

@app.route("/logs/sync-gdrive", methods=["POST"])
def sync_to_gdrive():
    SPREADSHEET_ID = "1tFVdPTekLhjVuvC36PvANdTbbM5-Y-JiR7cQ-per0Yg"
    CREDS_FILE     = "credentials.json"
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if not os.path.exists(LOG_FILE):
        add_alert("warning", "No sensor logs found to sync.")
        return redirect(url_for("logs_page"))
    if not os.path.exists(CREDS_FILE):
        add_alert("danger", "Google credentials file not found.")
        return redirect(url_for("logs_page"))
    try:
        creds  = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet  = client.open_by_key(SPREADSHEET_ID).sheet1
        rows   = []
        with open(LOG_FILE, "r") as f:
            reader = csv.reader(f)
            for row in reader:
                rows.append(row)
        sheet.clear()
        sheet.update(rows)
        add_alert("info", f"Synced {len(rows) - 1} log entries to Google Sheets.")
    except Exception as e:
        add_alert("danger", f"Google Sheets sync failed: {str(e)}")
    return redirect(url_for("logs_page"))

@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    initialize_csv()
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
    return jsonify(list(reversed(logs)))

@app.route("/api/stats")
def api_stats():
    load_batches()
    active_batches    = len([b for b in batches if b["status"] == "active"])
    completed_batches = len([b for b in batches if b["status"] == "completed"])
    unread_alerts     = len([a for a in alerts if not a["read"]])
    # Return current bin capacity from the latest sensor reading
    return jsonify({
        "active_batches":    active_batches,
        "completed_batches": completed_batches,
        "unread_alerts":     unread_alerts,
        "bin_capacity":      sensor_data.get("bin_capacity")   # % full, replaces total_weight
    })

# ------------------ START APP ------------------
if __name__ == "__main__":
    threading.Thread(target=background_task, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)