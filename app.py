from flask import (
    Flask, render_template, jsonify, request,
    redirect, url_for, send_file, session, Response
)
import datetime
import csv
import os
import threading
import time
import io
import platform
import shutil
import bcrypt
import resend  # Added Resend
from dotenv import load_dotenv
from supabase import create_client, Client
from functools import wraps

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
    print("[WARN] RPi.GPIO not found. Sensor readings will be simulated.")

# ------------------ CAMERA IMPORT ------------------
try:
    from picamera2 import Picamera2
    import libcamera
    CAMERA_AVAILABLE = True
except ImportError:
    CAMERA_AVAILABLE = False
    print("[WARN] picamera2 not found. Camera will be unavailable.")

# ------------------ ENV ------------------
load_dotenv()

SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
PASSPHRASE_HASH = os.getenv("SECRET_PASSPHRASE_HASH", "").encode()
FLASK_SECRET    = os.getenv("FLASK_SECRET_KEY", "changeme-set-in-env")
RESEND_API_KEY  = os.getenv("RESEND_API_KEY")
EMAIL_SENDER    = "Bokashi System <alerts@rpi.tail0f57db.ts.net>"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Resend
resend.api_key = RESEND_API_KEY

# ------------------ GPIO PIN CONFIGURATION ------------------
DHT_SENSOR_TYPE = Adafruit_DHT.DHT22 if DHT_AVAILABLE else None
DHT_PIN         = 4
TRIG_PIN        = 23
ECHO_PIN        = 24
WATER_PIN       = 17

BIN_HEIGHT_CM   = 40.0
MAX_DISTANCE_CM = 400.0

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
app.config['SECRET_KEY'] = FLASK_SECRET

# ------------------ CAMERA ------------------
camera      = None
camera_lock = threading.Lock()

def init_camera():
    global camera
    if not CAMERA_AVAILABLE:
        return
    try:
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": (320, 240), "format": "RGB888"},
            controls={"FrameRate": 5}
        )
        camera.configure(config)
        camera.start()
        time.sleep(1)
        print("[INFO] Camera started.")
    except Exception as e:
        print(f"[WARN] Camera init failed: {e}")
        camera = None

def capture_jpeg():
    if not CAMERA_AVAILABLE or camera is None:
        return None
    with camera_lock:
        try:
            buf = io.BytesIO()
            camera.capture_file(buf, format="jpeg")
            return buf.getvalue()
        except Exception as e:
            print(f"[WARN] Frame capture failed: {e}")
            return None

def generate_mjpeg():
    while True:
        frame = capture_jpeg()
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
        time.sleep(0.5)

# ------------------ TIMESTAMP HELPER ------------------
def now_local_iso():
    return datetime.datetime.now().isoformat()

# ------------------ EMAIL & NOTIFICATION LOGIC ------------------

def should_send_notification(notif_type, interval_minutes=60):
    """Checks Supabase to see if we've sent this type of email recently."""
    try:
        now = datetime.datetime.now()
        result = supabase.table("notification_logs").select("last_sent").eq("type", notif_type).execute()
        
        if result.data:
            last_sent_str = result.data[0]["last_sent"]
            last_sent = datetime.datetime.fromisoformat(last_sent_str.replace('Z', '+00:00'))
            if (now.astimezone() - last_sent.astimezone()).total_seconds() < (interval_minutes * 60):
                return False 
        
        supabase.table("notification_logs").upsert({
            "type": notif_type, 
            "last_sent": now.isoformat()
        }).execute()
        return True
    except Exception as e:
        print(f"[WARN] Throttling check failed: {e}")
        return True

def send_resend_email(subject, body, notif_type):
    """Sends email via Resend if enabled in settings and not throttled."""
    if not settings["notifications"]["email_enabled"]:
        return

    recipient = settings["notifications"]["email"]
    if not recipient:
        return

    if not should_send_notification(notif_type):
        return

    try:
        resend.Emails.send({
            "from": EMAIL_SENDER,
            "to": recipient,
            "subject": f"[Bokashi Alert] {subject}",
            "text": body
        })
        print(f"[INFO] Resend email sent: {notif_type}")
    except Exception as e:
        print(f"[ERROR] Resend failed: {e}")

# ------------------ IN-MEMORY STATE ------------------
sensor_data = {
    "temperature":  None,
    "humidity":     None,
    "bin_capacity": None,
    "water_status": None,
    "last_updated": None,
    "sensor_error": None
}

sensor_status = {
    "dht22":  "unknown",
    "hcsr04": "unknown",
    "hw038":  "unknown",
    "camera": "unknown"
}

settings = {
    "thresholds": {
        "max_temp":         35,
        "min_temp":         15,
        "max_humidity":     70,
        "min_humidity":     40,
        "max_bin_capacity": 90
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

IMAGES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static", "images", "captures"
)
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# ------------------ AUTH ------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        passphrase = request.form.get("passphrase", "").encode()
        if PASSPHRASE_HASH and bcrypt.checkpw(passphrase, PASSPHRASE_HASH):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Incorrect passphrase. Try again."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------ SUPABASE HELPERS ------------------

def db_load_settings():
    global settings
    try:
        rows = supabase.table("settings").select("key, value").execute().data
        for row in rows:
            key = row["key"]
            val = row["value"]
            if key == "logging" and "interval_seconds" in val:
                val["interval_seconds"] = int(val["interval_seconds"])
            if key in settings:
                settings[key] = val
    except Exception as e:
        print(f"[WARN] Could not load settings: {e}")

def db_save_setting(key, value):
    try:
        supabase.table("settings").upsert({"key": key, "value": value}).execute()
    except Exception as e:
        print(f"[WARN] Could not save setting '{key}': {e}")

def db_add_alert(level, message):
    try:
        supabase.table("alerts").insert({
            "timestamp": now_local_iso(),
            "level":     level,
            "message":   message,
            "read":      False
        }).execute()
    except Exception as e:
        print(f"[WARN] Could not insert alert: {e}")

def db_log_sensor(data):
    try:
        supabase.table("sensor_logs").insert({
            "timestamp":    now_local_iso(),
            "temperature":  data["temperature"],
            "humidity":     data["humidity"],
            "bin_capacity": data["bin_capacity"],
            "water_status": data["water_status"]
        }).execute()
    except Exception as e:
        print(f"[WARN] Could not log sensor data: {e}")

def db_save_image(filename, source="snapshot"):
    try:
        supabase.table("images").insert({
            "filename":    filename,
            "captured_at": now_local_iso(),
            "source":      source
        }).execute()
    except Exception as e:
        print(f"[WARN] Could not save image record: {e}")

# ------------------ SYSTEM INFO ------------------
def format_uptime(total_seconds):
    total_seconds = max(0, int(total_seconds))
    days, rem     = divmod(total_seconds, 86400)
    hours, rem    = divmod(rem, 3600)
    minutes, _    = divmod(rem, 60)
    return f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"

def get_system_uptime_seconds():
    try:
        if os.path.exists("/proc/uptime"):
            with open("/proc/uptime", "r") as f:
                return float(f.read().split()[0])
    except Exception:
        pass
    return time.time() - app_start_time

def get_cpu_temp_text():
    try:
        path = "/sys/class/thermal/thermal_zone0/temp"
        if os.path.exists(path):
            with open(path, "r") as f:
                return f"{float(f.read().strip()) / 1000:.1f}°C"
    except Exception:
        pass
    return "N/A"

def get_raspberry_model():
    try:
        path = "/proc/device-tree/model"
        if os.path.exists(path):
            with open(path, "r", errors="ignore") as f:
                return f.read().replace("\x00", "").strip()
    except Exception:
        pass
    return platform.platform()

def get_storage_text(path="/"):
    try:
        u = shutil.disk_usage(path)
        return f"{u.used / 1024**3:.1f} GB / {u.total / 1024**3:.1f} GB"
    except Exception:
        return "N/A"

# ------------------ SENSOR READS ------------------

def read_dht22():
    if not DHT_AVAILABLE:
        import random
        return round(28.0 + random.uniform(-1, 1), 1), round(65.0 + random.uniform(-2, 2), 1)
    humidity, temperature = Adafruit_DHT.read_retry(DHT_SENSOR_TYPE, DHT_PIN, retries=5)
    return temperature, humidity

def read_ultrasonic_cm():
    if not GPIO_AVAILABLE:
        import random
        return round(random.uniform(5, BIN_HEIGHT_CM), 1)
    GPIO.output(TRIG_PIN, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(TRIG_PIN, GPIO.LOW)
    timeout     = time.time() + 0.04
    pulse_start = time.time()
    while GPIO.input(ECHO_PIN) == GPIO.LOW:
        pulse_start = time.time()
        if time.time() > timeout:
            return None
    pulse_end = time.time()
    while GPIO.input(ECHO_PIN) == GPIO.HIGH:
        pulse_end = time.time()
        if time.time() > timeout:
            return None
    distance_cm = ((pulse_end - pulse_start) * 34300) / 2
    return round(distance_cm, 1) if distance_cm <= MAX_DISTANCE_CM else None

def distance_to_bin_capacity_pct(distance_cm):
    if distance_cm is None:
        return None
    return round(max(0.0, min(100.0, ((BIN_HEIGHT_CM - distance_cm) / BIN_HEIGHT_CM) * 100)), 1)

def read_water_sensor():
    if not GPIO_AVAILABLE:
        return "wet"
    return "wet" if GPIO.input(WATER_PIN) == GPIO.HIGH else "dry"

def read_sensors():
    previous_water_status = sensor_data.get("water_status")

    # DHT22
    temperature, humidity = read_dht22()
    if temperature is None or humidity is None:
        sensor_status["dht22"] = "offline"
        db_add_alert("warning", "DHT22 read failed – check GPIO4.")
    else:
        sensor_data["temperature"] = round(temperature, 1)
        sensor_data["humidity"]    = round(humidity, 1)
        sensor_status["dht22"]     = "online"

    # HC-SR04
    distance     = read_ultrasonic_cm()
    capacity_pct = distance_to_bin_capacity_pct(distance)
    if capacity_pct is None:
        sensor_status["hcsr04"] = "offline"
        db_add_alert("warning", "Ultrasonic timeout – check GPIO23/GPIO24.")
    else:
        sensor_data["bin_capacity"] = capacity_pct
        sensor_status["hcsr04"]     = "online"

    # HW-038
    try:
        sensor_data["water_status"] = read_water_sensor()
        sensor_status["hw038"]      = "online"
    except Exception:
        sensor_status["hw038"] = "offline"

    # Camera
    sensor_status["camera"] = "online" if (CAMERA_AVAILABLE and camera is not None) else "offline"

    # Error summary
    offline = [k for k, v in sensor_status.items() if v == "offline"]
    sensor_data["last_updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sensor_data["sensor_error"] = f"Offline: {', '.join(offline)}" if offline else None

    # --- EMAIL TRIGGERS ---
    
    # 1. Water Detection Alert
    if sensor_data["water_status"] == "wet" and previous_water_status != "wet":
        msg = "Water detected in the composter system. Check for leaks or drainage issues!"
        db_add_alert("danger", msg)
        send_resend_email("Bokashi tea detected", msg, "bokashi_tea")

    # 2. Hardware Disconnection Alert
    if offline:
        msg = f"Connectivity Alert: The following sensors are offline: {', '.join(offline)}"
        send_resend_email("Sensor Connection Error", msg, "sensor_offline")

    # Threshold alerts (Standard Alerts)
    th = settings.get("thresholds", {})
    if sensor_data["temperature"] is not None:
        if sensor_data["temperature"] > th.get("max_temp", 35):
            db_add_alert("warning", f"High temperature: {sensor_data['temperature']}°C")
        if sensor_data["temperature"] < th.get("min_temp", 15):
            db_add_alert("warning", f"Low temperature: {sensor_data['temperature']}°C")
    if sensor_data["humidity"] is not None:
        if sensor_data["humidity"] > th.get("max_humidity", 70):
            db_add_alert("warning", f"High humidity: {sensor_data['humidity']}%")
        if sensor_data["humidity"] < th.get("min_humidity", 40):
            db_add_alert("warning", f"Low humidity: {sensor_data['humidity']}%")
    if sensor_data["bin_capacity"] is not None:
        if sensor_data["bin_capacity"] > th.get("max_bin_capacity", 90):
            db_add_alert("danger", f"Bin almost full: {sensor_data['bin_capacity']}%")

# ------------------ BACKGROUND TASK ------------------
def background_task():
    setup_gpio()
    init_camera()
    db_load_settings()
    while True:
        db_load_settings()
        read_sensors()
        db_log_sensor(sensor_data)
        interval = settings.get("logging", {}).get("interval_seconds", 60)
        time.sleep(interval)

# ------------------ CAMERA ROUTES ------------------

@app.route("/camera/stream")
@login_required
def camera_stream():
    if not CAMERA_AVAILABLE or camera is None:
        return "Camera not available", 503
    return Response(generate_mjpeg(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/camera/snapshot", methods=["POST"])
@login_required
def camera_snapshot():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    if not CAMERA_AVAILABLE or camera is None:
        db_add_alert("warning", "Snapshot failed – camera not available.")
        return redirect(url_for("device_control"))
    frame = capture_jpeg()
    if frame is None:
        db_add_alert("warning", "Snapshot failed – could not capture frame.")
        return redirect(url_for("device_control"))
    filename = datetime.datetime.now().strftime("snap_%Y%m%d_%H%M%S.jpg")
    with open(os.path.join(IMAGES_DIR, filename), "wb") as f:
        f.write(frame)
    db_save_image(filename, source="snapshot")
    db_add_alert("info", f"Snapshot saved: {filename}")
    return redirect(url_for("view_images"))

# ------------------ PAGE ROUTES ------------------

@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", active_page="dashboard")

@app.route("/batches")
@login_required
def batch_management():
    try:
        batches = supabase.table("batches").select("*").order("id").execute().data
    except Exception:
        batches = []
    return render_template("batches.html", batches=batches, active_page="batches")

@app.route("/batches/add", methods=["POST"])
@login_required
def add_batch():
    data = request.form
    try:
        supabase.table("batches").insert({
            "name":       data.get("name"),
            "start_date": data.get("start_date"),
            "status":     "active",
            "weight":     float(data.get("weight", 0)),
            "notes":      data.get("notes", "")
        }).execute()
        db_add_alert("info", f"New batch '{data.get('name')}' created")
    except Exception as e:
        print(f"[WARN] add_batch: {e}")
    return redirect(url_for("batch_management"))

@app.route("/batches/complete/<int:batch_id>")
@login_required
def complete_batch(batch_id):
    try:
        supabase.table("batches").update({
            "status":   "completed",
            "end_date": datetime.datetime.now().strftime("%Y-%m-%d")
        }).eq("id", batch_id).execute()
        db_add_alert("info", "Batch marked as completed")
    except Exception as e:
        print(f"[WARN] complete_batch: {e}")
    return redirect(url_for("batch_management"))

@app.route("/logs-page")
@login_required
def logs_page():
    return render_template("logs.html", active_page="logs")

@app.route("/control")
@login_required
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
@login_required
def view_images():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    try:
        db_images = supabase.table("images").select("*").order("captured_at", desc=True).execute().data
    except Exception:
        db_images = []
    fs_images = get_capture_images()
    return render_template("images.html", db_images=db_images, fs_images=fs_images, active_page="control")

@app.route("/control/images/upload", methods=["POST"])
@login_required
def upload_image():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    file = request.files.get("image") or request.files.get("file")
    if not file or file.filename == "":
        return redirect(url_for("view_images"))
    ext = file.filename.lower().split(".")[-1]
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        db_add_alert("warning", "Invalid image type.")
        return redirect(url_for("view_images"))
    safe_name = datetime.datetime.now().strftime("upload_%Y%m%d_%H%M%S") + "." + ext
    file.save(os.path.join(IMAGES_DIR, safe_name))
    db_save_image(safe_name, source="upload")
    db_add_alert("info", f"Image uploaded: {safe_name}")
    return redirect(url_for("view_images"))

@app.route("/control/add-bran", methods=["POST"])
@login_required
def add_bran():
    amount = request.form.get("amount")
    db_add_alert("info", f"Added {amount}g of Bokashi bran")
    return redirect(url_for("device_control"))

@app.route("/alerts-page")
@login_required
def alerts_page():
    try:
        alerts = supabase.table("alerts").select("*").order("id", desc=True).limit(100).execute().data
    except Exception:
        alerts = []
    return render_template("alerts.html", alerts=alerts, active_page="alerts")

@app.route("/alerts/mark-read/<int:alert_id>")
@login_required
def mark_alert_read(alert_id):
    try:
        supabase.table("alerts").update({"read": True}).eq("id", alert_id).execute()
    except Exception as e:
        print(f"[WARN] mark_alert_read: {e}")
    return redirect(url_for("alerts_page"))

@app.route("/alerts/mark-all-read", methods=["POST"])
@login_required
def mark_all_alerts_read():
    try:
        supabase.table("alerts").update({"read": True}).eq("read", False).execute()
    except Exception as e:
        print(f"[WARN] mark_all_alerts_read: {e}")
    return redirect(url_for("alerts_page"))

@app.route("/settings", methods=["GET", "POST"], endpoint="settings")
@login_required
def settings_page():
    db_load_settings()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "thresholds":
            val = {
                "max_temp":         float(request.form.get("max_temp",         35)),
                "min_temp":         float(request.form.get("min_temp",         15)),
                "max_humidity":     float(request.form.get("max_humidity",     70)),
                "min_humidity":     float(request.form.get("min_humidity",     40)),
                "max_bin_capacity": float(request.form.get("max_bin_capacity", 90))
            }
            settings["thresholds"] = val
            db_save_setting("thresholds", val)
        elif section == "notifications":
            val = {
                "email_enabled": request.form.get("email_enabled") == "on",
                "sms_enabled":   request.form.get("sms_enabled")   == "on",
                "push_enabled":  request.form.get("push_enabled")  == "on",
                "email":         request.form.get("email",  ""),
                "phone":         request.form.get("phone",  "")
            }
            settings["notifications"] = val
            db_save_setting("notifications", val)
        elif section == "logging":
            val = {
                "interval_seconds": int(request.form.get("interval_seconds", 60)),
                "auto_backup":      request.form.get("auto_backup") == "on"
            }
            settings["logging"] = val
            db_save_setting("logging", val)
        db_add_alert("info", "Settings saved successfully")
        return redirect(url_for("settings"))

    system_info = {
        "device_name":     platform.node() or "Unknown",
        "version":         "1.0.0",
        "raspberry_model": get_raspberry_model(),
        "uptime":          format_uptime(get_system_uptime_seconds()),
        "cpu_temp":        get_cpu_temp_text(),
        "storage_used":    get_storage_text("/")
    }
    return render_template("settings.html", active_page="settings",
                           settings=settings, system_info=system_info)

# ------------------ API ROUTES ------------------

@app.route("/api/sensors")
@login_required
def api_sensors():
    return jsonify(sensor_data)

@app.route("/api/device-status")
@login_required
def api_device_status():
    return jsonify({"sensors": sensor_status})

@app.route("/api/stats")
@login_required
def api_stats():
    try:
        all_batches       = supabase.table("batches").select("status").execute().data
        active_batches    = sum(1 for b in all_batches if b["status"] == "active")
        completed_batches = sum(1 for b in all_batches if b["status"] == "completed")
        unread_alerts     = supabase.table("alerts").select("id", count="exact").eq("read", False).execute().count
    except Exception:
        active_batches = completed_batches = 0
        unread_alerts  = 0
    return jsonify({
        "active_batches":    active_batches,
        "completed_batches": completed_batches,
        "unread_alerts":     unread_alerts or 0,
        "bin_capacity":      sensor_data.get("bin_capacity")
    })

@app.route("/logs")
@login_required
def api_logs():
    try:
        logs = supabase.table("sensor_logs").select("*").order("id", desc=True).limit(200).execute().data
    except Exception:
        logs = []
    return jsonify(logs)

@app.route("/logs/export")
@login_required
def export_logs_csv():
    try:
        logs = supabase.table("sensor_logs").select("*").order("id", desc=True).execute().data
    except Exception:
        logs = []
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Temperature", "Humidity", "Bin_Capacity_Pct", "Water_Status"])
    for row in logs:
        writer.writerow([row.get("timestamp"), row.get("temperature"),
                         row.get("humidity"), row.get("bin_capacity"), row.get("water_status")])
    buffer = io.BytesIO(output.getvalue().encode("utf-8"))
    buffer.seek(0)
    return send_file(buffer, mimetype="text/csv", as_attachment=True,
                     download_name=f"bokashi_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.csv")

@app.route("/control/test-alert", methods=["POST"])
@login_required
def test_alert():
    db_add_alert("warning", "This is a test alert from Device Control.")
    return redirect(url_for("device_control"))

# ------------------ START ------------------
if __name__ == "__main__":
    threading.Thread(target=background_task, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)