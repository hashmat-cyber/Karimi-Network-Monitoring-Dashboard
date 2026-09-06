from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import subprocess
import platform
import re
import os
import csv
import io
import ipaddress
import concurrent.futures

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + os.path.join(BASE_DIR, "network.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ============================================================
# DATABASE
# ============================================================

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(120), nullable=False)
    device_type = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(30), default="Unknown")
    response_time = db.Column(db.Float, nullable=True)
    last_checked = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    history = db.relationship(
        "MonitoringHistory",
        backref="device",
        lazy=True,
        cascade="all, delete-orphan"
    )


class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_name = db.Column(db.String(120), nullable=False)
    message = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(30), default="Medium")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved = db.Column(db.Boolean, default=False)


class MonitoringHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer,
        db.ForeignKey("device.id"),
        nullable=False
    )
    status = db.Column(db.String(30), nullable=False)
    response_time = db.Column(db.Float, nullable=True)
    checked_at = db.Column(db.DateTime, default=datetime.utcnow)


# ============================================================
# PING
# ============================================================

def ping_device(ip):
    system = platform.system().lower()

    try:
        if system == "windows":
            command = [
                "ping",
                "-n",
                "1",
                "-w",
                "1000",
                ip
            ]
        else:
            command = [
                "ping",
                "-c",
                "1",
                "-W",
                "1",
                ip
            ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3
        )

        output = result.stdout + result.stderr

        if result.returncode != 0:
            return False, None

        match = re.search(
            r"time[=<]\s*([0-9]+(?:\.[0-9]+)?)\s*ms",
            output,
            re.IGNORECASE
        )

        if match:
            return True, float(match.group(1))

        return True, None

    except Exception:
        return False, None


# ============================================================
# ALERT SYSTEM
# ============================================================

def create_offline_alert(device):

    existing = Alert.query.filter_by(
        device_name=device.name,
        resolved=False
    ).first()

    if not existing:

        alert = Alert(
            device_name=device.name,
            message=(
                f"{device.name} ({device.ip_address}) "
                f"is offline."
            ),
            severity="High"
        )

        db.session.add(alert)


# ============================================================
# CHECK DEVICE
# ============================================================

def check_device(device):

    previous_status = device.status

    online, response_time = ping_device(
        device.ip_address
    )

    device.status = (
        "Online"
        if online
        else "Offline"
    )

    device.response_time = response_time
    device.last_checked = datetime.utcnow()

    history = MonitoringHistory(
        device_id=device.id,
        status=device.status,
        response_time=response_time,
        checked_at=datetime.utcnow()
    )

    db.session.add(history)

    if device.status == "Offline":
        create_offline_alert(device)

    if (
        previous_status == "Offline"
        and device.status == "Online"
    ):

        alerts = Alert.query.filter_by(
            device_name=device.name,
            resolved=False
        ).all()

        for alert in alerts:
            alert.resolved = True

    return device


def check_all_devices():

    devices = Device.query.order_by(
        Device.id.asc()
    ).all()

    for device in devices:
        check_device(device)

    db.session.commit()

    return devices


# ============================================================
# UPTIME CALCULATION
# ============================================================

def uptime_for_device(device, hours=24):

    since = datetime.utcnow() - timedelta(
        hours=hours
    )

    records = MonitoringHistory.query.filter(
        MonitoringHistory.device_id == device.id,
        MonitoringHistory.checked_at >= since
    ).all()

    if not records:
        return None

    online_count = sum(
        1 for record in records
        if record.status == "Online"
    )

    return round(
        (online_count / len(records)) * 100,
        2
    )


def average_response(device, hours=24):

    since = datetime.utcnow() - timedelta(
        hours=hours
    )

    records = MonitoringHistory.query.filter(
        MonitoringHistory.device_id == device.id,
        MonitoringHistory.checked_at >= since,
        MonitoringHistory.response_time.isnot(None)
    ).all()

    if not records:
        return None

    values = [
        record.response_time
        for record in records
    ]

    return round(
        sum(values) / len(values),
        2
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():

    devices = Device.query.order_by(
        Device.id.asc()
    ).all()

    total = len(devices)

    online = sum(
        1 for d in devices
        if d.status == "Online"
    )

    offline = sum(
        1 for d in devices
        if d.status == "Offline"
    )

    unknown = sum(
        1 for d in devices
        if d.status == "Unknown"
    )

    active_alerts = Alert.query.filter_by(
        resolved=False
    ).count()

    recent_alerts = Alert.query.order_by(
        Alert.created_at.desc()
    ).limit(5).all()

    recent_history = MonitoringHistory.query.order_by(
        MonitoringHistory.checked_at.desc()
    ).limit(10).all()

    device_stats = []

    for device in devices:

        device_stats.append({
            "id": device.id,
            "name": device.name,
            "uptime": uptime_for_device(device),
            "average_response": average_response(device)
        })

    return render_template(
        "dashboard.html",
        devices=devices,
        total=total,
        online=online,
        offline=offline,
        unknown=unknown,
        active_alerts=active_alerts,
        recent_alerts=recent_alerts,
        recent_history=recent_history,
        device_stats=device_stats,

        # Dashboard aliases
        total_devices=total,
        online_devices=online,
        offline_devices=offline,

        # Only latest 10 monitoring records are displayed
        recent_activity=recent_history[:10]
    )


# ============================================================
# DEVICES
# ============================================================

@app.route("/devices")
def devices():

    device_list = Device.query.order_by(
        Device.id.asc()
    ).all()

    return render_template(
        "devices.html",
        devices=device_list
    )


@app.post("/devices/add")
def add_device():

    name = request.form.get(
        "name",
        ""
    ).strip()

    ip_address = request.form.get(
        "ip_address",
        ""
    ).strip()

    device_type = request.form.get(
        "device_type",
        "Other"
    ).strip()

    if name and ip_address:

        device = Device(
            name=name,
            ip_address=ip_address,
            device_type=device_type,
            status="Unknown"
        )

        db.session.add(device)
        db.session.commit()

    return redirect(
        url_for("devices")
    )


@app.post("/devices/delete/<int:device_id>")
def delete_device(device_id):

    device = Device.query.get_or_404(
        device_id
    )

    db.session.delete(device)
    db.session.commit()

    return redirect(
        url_for("devices")
    )


@app.route("/devices/check/<int:device_id>")
def check_single_device(device_id):

    device = Device.query.get_or_404(
        device_id
    )

    check_device(device)

    db.session.commit()

    return redirect(
        url_for("devices")
    )


@app.route("/devices/check-all")
def check_all():

    check_all_devices()

    return redirect(
        url_for("devices")
    )


# ============================================================
# DEVICE DETAILS
# ============================================================

@app.route("/device/<int:device_id>")
def device_details(device_id):

    device = Device.query.get_or_404(
        device_id
    )

    history = MonitoringHistory.query.filter_by(
        device_id=device.id
    ).order_by(
        MonitoringHistory.checked_at.desc()
    ).limit(100).all()

    uptime = uptime_for_device(
        device,
        24
    )

    avg_response = average_response(
        device,
        24
    )

    return render_template(
        "device_details.html",
        device=device,
        history=history,
        uptime=uptime,
        avg_response=avg_response
    )


# ============================================================
# NETWORK SCANNER
# ============================================================

def scan_ip(ip):

    online, response = ping_device(
        str(ip)
    )

    return {
        "ip": str(ip),
        "online": online,
        "response_time": response
    }


@app.route("/scanner", methods=["GET", "POST"])
def scanner():

    results = []
    error = None
    network = ""

    if request.method == "POST":

        network = request.form.get(
            "network",
            ""
        ).strip()

        try:

            parsed_network = ipaddress.ip_network(
                network,
                strict=False
            )

            # Security/performance limit
            if parsed_network.num_addresses > 256:
                error = (
                    "Network too large. "
                    "Please scan a /24 or smaller network."
                )

            else:

                addresses = list(
                    parsed_network.hosts()
                )

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=32
                ) as executor:

                    results = list(
                        executor.map(
                            scan_ip,
                            addresses
                        )
                    )

        except ValueError:

            error = (
                "Invalid network format. "
                "Example: 192.168.1.0/24"
            )

    online_count = sum(
        1 for item in results
        if item["online"]
    )

    return render_template(
        "scanner.html",
        results=results,
        error=error,
        network=network,
        online_count=online_count,
        scanned_count=len(results)
    )


# ============================================================
# REPORT
# ============================================================

@app.route("/reports")
def reports():

    devices = Device.query.order_by(
        Device.id.asc()
    ).all()

    report_data = []

    for device in devices:

        report_data.append({
            "name": device.name,
            "ip": device.ip_address,
            "type": device.device_type,
            "status": device.status,
            "uptime": uptime_for_device(device),
            "average_response": average_response(device),
            "last_checked": (
                device.last_checked.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if device.last_checked
                else "Never"
            )
        })

    total_checks = MonitoringHistory.query.count()

    total_alerts = Alert.query.count()

    return render_template(
        "reports.html",
        report_data=report_data,
        total_checks=total_checks,
        total_alerts=total_alerts
    )


@app.route("/reports/export")
def export_report():

    devices = Device.query.order_by(
        Device.id.asc()
    ).all()

    output = io.StringIO()

    writer = csv.writer(
        output
    )

    writer.writerow([
        "Device",
        "IP Address",
        "Type",
        "Status",
        "Uptime 24h (%)",
        "Average Response (ms)",
        "Last Checked"
    ])

    for device in devices:

        uptime = uptime_for_device(
            device
        )

        avg = average_response(
            device
        )

        writer.writerow([
            device.name,
            device.ip_address,
            device.device_type,
            device.status,
            uptime if uptime is not None else "N/A",
            avg if avg is not None else "N/A",
            (
                device.last_checked.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if device.last_checked
                else "Never"
            )
        ])

    response = Response(
        output.getvalue(),
        mimetype="text/csv"
    )

    response.headers[
        "Content-Disposition"
    ] = (
        "attachment; filename="
        "karimi-network-report.csv"
    )

    return response


# ============================================================
# ALERTS
# ============================================================

@app.route("/alerts")
def alerts():

    all_alerts = Alert.query.order_by(
        Alert.created_at.desc()
    ).all()

    return render_template(
        "alerts.html",
        alerts=all_alerts
    )


@app.post("/alerts/resolve/<int:alert_id>")
def resolve_alert(alert_id):

    alert = Alert.query.get_or_404(
        alert_id
    )

    alert.resolved = True

    db.session.commit()

    return redirect(
        url_for("alerts")
    )


@app.post("/alerts/clear")
def clear_resolved_alerts():

    Alert.query.filter_by(
        resolved=True
    ).delete()

    db.session.commit()

    return redirect(
        url_for("alerts")
    )


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():

    devices = check_all_devices()

    result = []

    for device in devices:

        result.append({
            "id": device.id,
            "name": device.name,
            "ip": device.ip_address,
            "type": device.device_type,
            "status": device.status,
            "response_time": device.response_time,
            "uptime": uptime_for_device(device),
            "average_response": average_response(device),
            "last_checked": (
                device.last_checked.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if device.last_checked
                else None
            )
        })

    return jsonify({
        "success": True,
        "timestamp": datetime.utcnow().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "total": len(devices),
        "online": sum(
            1 for d in devices
            if d.status == "Online"
        ),
        "offline": sum(
            1 for d in devices
            if d.status == "Offline"
        ),
        "active_alerts": Alert.query.filter_by(
            resolved=False
        ).count(),
        "devices": result
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": (
            "Karimi Network Monitoring Dashboard"
        ),
        "database": "SQLite",
        "monitoring": "active",
        "scanner": "available",
        "reports": "available",
        "timestamp": datetime.utcnow().isoformat()
    })


# ============================================================
# DATABASE
# ============================================================

def initialize_database():

    db.create_all()

    if Device.query.count() == 0:

        local = Device(
            name="Local Computer",
            ip_address="127.0.0.1",
            device_type="Computer"
        )

        router = Device(
            name="Main Router",
            ip_address="192.168.1.1",
            device_type="Router"
        )

        db.session.add(local)
        db.session.add(router)

        db.session.commit()


with app.app_context():
    initialize_database()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )


