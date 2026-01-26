from flask import Flask, render_template, jsonify
import datetime
import csv
import os
import threading
import time

app = Flask(__name__)

# ------------------ SENSOR PLACEHOLDERS ------------------
sensor_data = {
    "temperature": 30.0,
    "humidity": 65.0,
    "ammonia": 0.2,
    "last_updated": None
}

LOG_FILE = "sensor_log.csv"


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
    # Replace this later with real sensors
    sensor_data["temperature"] += 0.1
    sensor_data["humidity"] -= 0.1
    sensor_data["ammonia"] += 0.01
    sensor_data["last_updated"] = datetime.datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ------------------ LOGGING ------------------
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
    while True:
        read_sensors()
        log_sensor_data()
        time.sleep(5)


# ------------------ FLASK ROUTES ------------------
@app.route("/")
def dashboard():
    return render_template("dashboard.html")


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
    return jsonify(logs)


# ------------------ START APP ------------------
if __name__ == "__main__":
    threading.Thread(target=background_task, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
