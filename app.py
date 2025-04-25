import sys
import subprocess

def _ensure_installed(pkg_name, import_name=None):
    """
    Try to import `import_name` (or pkg_name if None); if missing, pip-install pkg_name.
    """
    try:
        __import__(import_name or pkg_name)
    except ImportError:
        print(f"Installing {pkg_name}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_name])

_ensure_installed("SQLAlchemy", "sqlalchemy")
_ensure_installed("pyodbc")
_ensure_installed("gspread")
_ensure_installed("oauth2client")

from flask import Flask, render_template, request, redirect, url_for, flash
import os
import re
import json
import time
import serial
import serial.tools.list_ports
from serial_sender import send_file
from datetime import datetime, timedelta
import threading
from sqlalchemy import create_engine, Table, Column, Integer, String, DateTime, MetaData
import gspread
from oauth2client.service_account import ServiceAccountCredentials




UPLOAD_FOLDER = "uploads/haas1"
CONFIG_FILE = "serial_config.json"
MACHINE_INFO_FILE = "machine_info.json"
LOG_FILE = "transfer_log.txt"
MONITOR_LOG_FILE = "cnc_monitor_log.txt"
CONFIG_PATH = "q600_config.json"
LOG_CONFIG = "log_config.json"


last_static_run = None

static_commands = {
    "Q100": "Serial Number",
    "Q101": "Software Version",
    "Q102": "Model Number"
}

dynamic_commands = {
    "?Q104": "Machine Mode",
    "?Q200": "Tool Changes",
    "?Q201": "Tool In Use",
    "?Q300": "Power-On Time",
    "?Q301": "Motion Time",
    "?Q303": "Last Cycle Time",
    "?Q304": "Previous Cycle Time",
    "?Q402": "M30 Counter #1",
    "?Q403": "M30 Counter #2",
    "?Q500": "Program/Status/Parts",

}
q600_variables = {
    # General machine status
    "Feed Timer": (3022, "Total feed time used on current part"),
    "Tool in Spindle": (3026, "Current tool number in the spindle"),

    # M30 counters
    "M30 Counter #1": (3901, "Parts completed counter 1"),
    "M30 Counter #2": (3902, "Parts completed counter 2"),

    # ATM Group Info
    "ATM Group ID": (8500, "ATM tool group identifier"),
    "ATM % Tool Life (All Tools)": (8501, "Percent of available tool life across all tools in the group"),
    "ATM Tool Usage Count (All Tools)": (8502, "Total available usage count for the group"),
    "ATM Hole Count (All Tools)": (8503, "Total available hole count in group"),
    "ATM Feed Time (All Tools)": (8504, "Feed time available across all tools in group (seconds)"),
    "ATM Total Time (All Tools)": (8505, "Total available tool time in group (seconds)"),

    # ATM Next Tool Info
    "ATM Next Tool Number": (8510, "Next tool number to be used"),
    "ATM % Tool Life (Next Tool)": (8511, "Tool life remaining for next tool (percent)"),
    "ATM Usage Count (Next Tool)": (8512, "Usage count remaining for next tool"),
    "ATM Hole Count (Next Tool)": (8513, "Remaining holes allowed on next tool"),
    "ATM Feed Time (Next Tool)": (8514, "Remaining feed time on next tool (seconds)"),
    "ATM Total Time (Next Tool)": (8515, "Remaining total time on next tool (seconds)"),

    # ATM Tool Metadata
    "ATM Tool ID": (8550, "Tool ID currently in use"),
    "ATM Tool Flutes": (8551, "Number of flutes on the tool"),
    "ATM Max Vibration": (8552, "Maximum recorded vibration"),
    "ATM Tool Length Offset": (8553, "Tool length offset"),
    "ATM Tool Length Wear": (8554, "Tool length wear value"),
    "ATM Tool Diameter Offset": (8555, "Tool diameter or radius offset"),
    "ATM Tool Diameter Wear": (8556, "Tool diameter wear value"),
    "ATM Actual Diameter": (8557, "Actual measured diameter of the tool"),
    "ATM Coolant Position": (8558, "Coolant nozzle position for this tool"),
    "ATM Tool Feed Timer": (8559, "Time tool has been under feed (seconds)"),
    "ATM Total Timer": (8560, "Total usage time for this tool (seconds)"),
    "ATM Life Limit": (8561, "Tool life limit for this tool"),
    "ATM Life Counter": (8562, "Current tool life counter value"),
    "ATM Load Monitor Max": (8563, "Maximum spindle load recorded"),
    "ATM Load Limit": (8564, "Spindle load limit for tool"),

    # Analog Inputs / Load Data
    "Max Axis Load (X–B)": (list(range(1064, 1069)), "Max load on axes X, Y, Z, A, B"),
    "Axis Load (C–T)": (list(range(1264, 1269)), "Max load on axes C, U, V, W, T"),
    "Raw Analog Inputs": (list(range(1080, 1088)), "Raw voltage input per axis"),
    "Filtered Analog Inputs": (list(range(1090, 1099)), "Filtered analog signals (post-processing)"),

    # Tooling Ranges
    "Tool Flutes": (list(range(1601, 1801)), "Number of flutes for tools 1–200"),
    "Tool Vibrations": (list(range(1801, 2001)), "Max vibration of tools 1–200"),
    "Tool Length Offsets": (list(range(2001, 2201)), "Length offset for each tool"),
    "Tool Length Wear": (list(range(2201, 2401)), "Length wear compensation values"),
    "Tool Diameter Offsets": (list(range(2401, 2601)), "Diameter/radius offset for tools"),
    "Tool Diameter Wear": (list(range(2601, 2801)), "Diameter/radius wear compensation"),

    # Tool Timers & Monitoring
    "Tool Feed Timers": (list(range(5401, 5501)), "Feed time for each tool (seconds)"),
    "Tool Total Timers": (list(range(5501, 5601)), "Total usage time of each tool (seconds)"),
    "Tool Life Limits": (list(range(5601, 5701)), "Life limit of each tool"),
    "Tool Life Counters": (list(range(5701, 5801)), "Current tool life counters"),
    "Tool Load Monitor Max": (list(range(5801, 5901)), "Max load seen by tool"),
    "Tool Load Monitor Limit": (list(range(5901, 6001)), "Configured load limit per tool")
}

def get_program_name(filepath):
    prog_re = re.compile(r'^\s*O(\d+)\s*(.*)', re.IGNORECASE)
    with open(filepath, 'r', errors='ignore') as f:
        for line in f:
            m = prog_re.match(line)
            if m:
                num, name = m.group(1), m.group(2).strip()
                return f"{num} {name}" if name else num
    return os.path.splitext(os.path.basename(filepath))[0]


serial_lock = threading.Lock()

def load_serial_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {
        "port": "/dev/ttyUSB0",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "None",
        "stopbits": 1
    }

def save_serial_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def load_machine_info():
    if os.path.exists(MACHINE_INFO_FILE):
        with open(MACHINE_INFO_FILE, "r") as f:
            return json.load(f)
    return {
        "name": "Haas VF2",
        "manufacturer": "Haas",
        "polling_rate": 5
    }

def save_machine_info(info):
    with open(MACHINE_INFO_FILE, "w") as f:
        json.dump(info, f, indent=4)

def load_q600_config():
    if os.path.exists(CONFIG_PATH):
        return json.load(open(CONFIG_PATH))
    # default: no selections
    return {"q600_selected": []}

def save_q600_config(selected_labels):
    data = {"q600_selected": selected_labels}
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=4)

def load_log_config():
    if os.path.exists(LOG_CONFIG):
        return json.load(open(LOG_CONFIG))
    return {
      "backend": "sheet",
      "sheet_id": "<YOUR_DEFAULT_SHEET_ID>",
      "sql_conn": "",
      "service_account_info": {}
    }

def save_log_config(cfg):
    with open(LOG_CONFIG, "w") as f:
        json.dump(cfg, f, indent=4)

def append_log(message):
    with open(LOG_FILE, "a") as log:
        log.write(message + "\n")

def read_log():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, "r") as log:
        return log.readlines()[-20:]

def send_q_command(ser, command):
    if not command.endswith('\r'):
        command += '\r'
    ser.write(command.encode('ascii'))
    time.sleep(0.3)
    return ser.read_all().decode('ascii', errors='ignore').strip()

def log_entry(tag, command, response, program_name=None):
    """
    Write one line to the CNC monitor log, including the current program name if provided.
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{timestamp} | {tag} | {command} -> {response}"
    if program_name:
        line += f" | Program: {program_name}"
    print(line)
    with open(MONITOR_LOG_FILE, 'a') as f:
        f.write(line + '\n')


def update_machine_info_from_q_commands(serial_config):
    def clean_response(response):
        lines = response.splitlines()
        for line in lines:
            line = line.strip()
            if "," in line:
                return line.split(",", 1)[1].strip()
        return response.strip()

    try:
        with serial.Serial(
            port=serial_config["port"],
            baudrate=serial_config["baudrate"],
            bytesize=serial_config["bytesize"],
            parity=serial_config["parity"],
            stopbits=serial_config["stopbits"],
            timeout=1,
            xonxoff=True
        ) as ser:
            def query(cmd):
                ser.write((cmd + "\r").encode("ascii"))
                time.sleep(0.3)
                return ser.read_all().decode("ascii", errors="ignore").strip()

            serial_number = clean_response(query("Q100"))
            software_version = clean_response(query("Q101"))
            model_number = clean_response(query("Q102"))

            machine_info = load_machine_info()
            machine_info.update({
                "serial_number": serial_number,
                "software_version": software_version,
                "model_number": model_number
            })

            save_machine_info(machine_info)

    except Exception as e:
        print(f"[WARN] Could not auto-update machine info: {e}")

def monitor_machine(machine_info, serial_config,
                    latest_readings,
                    log_cfg, sql_engine, sql_table, sheet_client):
    """
    Polls static & dynamic commands, updates latest_readings,
    and writes each reading to SQL or Google Sheets if configured,
    tagging each log line with the current program name (from Q500).
    """
    update_machine_info_from_q_commands(serial_config)

    parity_map = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "None": serial.PARITY_NONE,
        None: serial.PARITY_NONE
    }

    def monitor_loop():
        program_name = None
        all_cmds = {**static_commands, **dynamic_commands}

        while True:
            batch_rows = []
            try:
                with serial_lock:
                    raw_par = serial_config.get("parity", "N")
                    par = parity_map.get(raw_par, serial.PARITY_NONE)

                    with serial.Serial(
                        port=serial_config["port"],
                        baudrate=serial_config["baudrate"],
                        bytesize=serial_config["bytesize"],
                        parity=par,
                        stopbits=serial_config["stopbits"],
                        timeout=1,
                        xonxoff=True
                    ) as ser:

                        for cmd, tag in all_cmds.items():
                            result = send_q_command(ser, cmd)
                            timestamp = datetime.now()

                            # capture the program header from Q500
                            if tag == "Program/Status/Parts":
                                program_name = result

                            # log each reading with the current program_name
                            log_entry(tag, cmd, result, program_name)

                            # Update in-memory for UI
                            latest_readings[tag] = {
                                "value":     result,
                                "timestamp": timestamp.isoformat()
                            }

                            # Persist immediately to SQL if configured
                            if log_cfg.get("backend") == "sql" and sql_engine and sql_table:
                                ins = sql_table.insert().values(
                                    tag=tag, value=result, polled_at=timestamp
                                )
                                sql_engine.execute(ins)

                            # Otherwise, queue up for Sheets
                            elif log_cfg.get("backend") == "sheet" and sheet_client:
                                batch_rows.append([
                                    timestamp.isoformat(),
                                    tag,
                                    result
                                ])

            except Exception as e:
                print(f"[MONITOR] Failed: {e}")

            # Batch-write all queued rows to Google Sheets
            if log_cfg.get("backend") == "sheet" and sheet_client and batch_rows:
                sheet_client.values_append(
                    "Sheet1!A:C",
                    params={"valueInputOption": "RAW"},
                    body={"values": batch_rows}
                )

            # Sleep until next poll
            time.sleep(machine_info.get("polling_rate", 5))

    threading.Thread(target=monitor_loop, daemon=True).start()




def init_sql_engine(conn_str):
    engine = create_engine(f"mssql+pyodbc:///?odbc_connect={conn_str}")
    meta = MetaData()
    poll_table = Table('machine_poll', meta,
        Column('id', Integer, primary_key=True),
        Column('tag', String(128)),
        Column('value', String(256)),
        Column('polled_at', DateTime),
    )
    meta.create_all(engine)
    return engine, poll_table

def init_sheet_client(sheet_id, service_account_info):
    scope = [
      "https://spreadsheets.google.com/feeds",
      "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        service_account_info, scope
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id).sheet1

def init_sheet_client_from_dict(service_account_info):
    """
    Build a gspread client from a dict (no file upload needed).
    """
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(service_account_info, scope)
    return gspread.authorize(creds)





def create_app():
    app = Flask(__name__)
    app.secret_key = "cnc-secret-key"

    # Core configuration
    app.config["UPLOAD_FOLDER"]   = UPLOAD_FOLDER
    app.config["SERIAL_CONFIG"]   = load_serial_config()
    app.config["MACHINE_INFO"]    = load_machine_info()
    app.config["LATEST_READINGS"] = {}

    # Load (or default) logging settings; do NOT init back-ends yet
    log_cfg = load_log_config()
    app.config["LOG_CONFIG"] = log_cfg

    # Start the monitor thread; it will skip persistence until back-ends are configured
    monitor_machine(
        app.config["MACHINE_INFO"],
        app.config["SERIAL_CONFIG"],
        app.config["LATEST_READINGS"],
        log_cfg,
        None,  # sql_engine
        None,  # sql_table
        None   # sheet_client
    )

    @app.route("/", methods=["GET", "POST"])
    def index():
        log_cfg = app.config["LOG_CONFIG"]
        if request.method == "POST":
            # --- File Upload ---
            if "file" in request.files:
                f = request.files["file"]
                if f and f.filename.endswith((".nc", ".txt", ".gcode")):
                    dest = os.path.join(app.config["UPLOAD_FOLDER"], f.filename)
                    f.save(dest)

                    # extract and flash program name
                    prog_name = get_program_name(dest)
                    flash(f"Uploaded {f.filename} → Program: {prog_name}", "success")
                    append_log(f"Uploaded {f.filename} (Program: {prog_name})")

            # --- Serial Settings ---
            # (unchanged)
            if "baudrate" in request.form:
                cfg = app.config["SERIAL_CONFIG"]
                cfg.update({
                    "baudrate": int(request.form["baudrate"]),
                    "bytesize": int(request.form["bytesize"]),
                    "parity": request.form["parity"],
                    "stopbits": int(request.form["stopbits"])
                })
                save_serial_config(cfg)
                flash("Serial settings updated", "info")

            # --- Machine Info ---
            if "machine_name" in request.form:
                mi = app.config["MACHINE_INFO"]
                mi["name"] = request.form["machine_name"]
                mi["manufacturer"] = request.form["manufacturer"]
                if "polling_rate" in request.form:
                    mi["polling_rate"] = int(request.form["polling_rate"])
                save_machine_info(mi)
                flash("Machine info updated", "info")

            # --- Logging Settings ---
            if "log_backend" in request.form:
                log_cfg["backend"] = request.form["log_backend"]
                log_cfg["sql_conn"] = request.form.get("sql_conn", "").strip()
                log_cfg["sheet_id"] = request.form.get("sheet_id", "").strip()


                save_log_config(log_cfg)
                flash("Logging settings updated", "info")

                # Initialize the chosen back-end immediately
                if log_cfg["backend"] == "sql" and log_cfg["sql_conn"]:
                    eng, tbl = init_sql_engine(log_cfg["sql_conn"])
                    app.config["LOG_ENGINE"] = eng
                    app.config["LOG_TABLE"] = tbl
                    app.config.pop("LOG_SHEET", None)
                elif log_cfg["backend"] == "sheet" \
                        and log_cfg.get("service_account_info") \
                        and log_cfg["sheet_id"]:
                    sht = init_sheet_client(
                        log_cfg["sheet_id"],
                        log_cfg["service_account_info"]
                    )
                    app.config["LOG_SHEET"] = sht
                    app.config.pop("LOG_ENGINE", None)
                    app.config.pop("LOG_TABLE", None)

        # gather files & recent transfer log
        files = os.listdir(app.config["UPLOAD_FOLDER"])
        log_entries = read_log()

        # — — — EXISTING TAB CHECK — — —
        has_tab = False
        if log_cfg.get("backend") == "sheet" \
                and log_cfg.get("service_account_info") \
                and log_cfg.get("sheet_id"):
            try:
                client = init_sheet_client_from_dict(log_cfg["service_account_info"])
                spreadsheet = client.open_by_key(log_cfg["sheet_id"])
                titles = [ws.title for ws in spreadsheet.worksheets()]
                has_tab = app.config["MACHINE_INFO"]["name"] in titles
            except Exception:
                has_tab = False

        return render_template(
            "index.html",
            files=files,
            config=app.config["SERIAL_CONFIG"],
            log=log_entries,
            machine=app.config["MACHINE_INFO"],
            log_config=log_cfg,
            has_tab=has_tab
        )

    @app.route("/send/<filename>")
    def send(filename):
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)

        # 1) extract program name
        prog_name = get_program_name(filepath)
        flash(f"Sending program: {prog_name}", "info")

        try:
            # 2) stream the file
            with serial_lock:
                send_file(filepath, **app.config["SERIAL_CONFIG"])

            # 3) update machine info & log
            update_machine_info_from_q_commands(app.config["SERIAL_CONFIG"])
            flash(f"Sent {filename} successfully.", "success")
            append_log(f"Sent: {filename} ({prog_name})")
        except Exception as e:
            flash(f"Failed to send {filename}: {e}", "danger")
            append_log(f"Failed: {filename} - {e}")

        return redirect(url_for("index"))

    @app.route("/delete/<filename>", methods=["POST"])
    def delete_file(filename):
        # secure against path traversal
        uploads = app.config["UPLOAD_FOLDER"]
        filepath = os.path.join(uploads, filename)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                flash(f"Deleted {filename}", "warning")
                append_log(f"Deleted: {filename}")
            else:
                flash(f"File not found: {filename}", "danger")
        except Exception as e:
            flash(f"Error deleting {filename}: {e}", "danger")
        return redirect(url_for("index"))

    @app.route("/variables", methods=["GET", "POST"])
    def variable_config():
        cfg = load_q600_config()
        selected = cfg.get("q600_selected", [])
        if request.method == "POST":
            selected = request.form.getlist("q600_selected")
            save_q600_config(selected)
            flash("Q600 monitoring variables updated", "info")
            return redirect(url_for("variable_config"))
        return render_template(
            "variables.html",
            q600_variables=q600_variables,
            selected_q600=selected
        )

    @app.route("/livepolling")
    def livepolling():
        return render_template(
            "livepolling.html",
            machine=app.config["MACHINE_INFO"],
            static_commands=static_commands,
            dynamic_commands=dynamic_commands,
            latest_readings=app.config["LATEST_READINGS"]
        )

    @app.route("/add_machine", methods=["POST"])
    def add_machine():
        log_cfg = app.config["LOG_CONFIG"]
        sa_info = log_cfg.get("service_account_info")
        sheet_id = log_cfg.get("sheet_id")
        machine = request.form.get("machine_name", "").strip()

        if not sa_info or not sheet_id:
            flash("You must paste Service-Account JSON and create a sheet first.", "danger")
            return redirect(url_for("index"))

        if not machine:
            flash("Please enter a machine name.", "warning")
            return redirect(url_for("index"))

        try:
            # 1) Open the spreadsheet
            client = init_sheet_client_from_dict(sa_info)
            sh = client.open_by_key(sheet_id)

            # 2) Build a header row with every field you poll
            all_tags = list(static_commands.values()) + list(dynamic_commands.values())
            all_q600 = list(q600_variables.keys())
            header = ["Timestamp"] + all_tags + all_q600

            # 3) Create the worksheet with exactly enough columns
            sh.add_worksheet(
                title=machine,
                rows="1000",
                cols=str(len(header))
            )
            ws = sh.worksheet(machine)

            # 4) Write the header row
            ws.append_row(header)

            flash(f"Created tab “{machine}” with {len(header)} columns.", "success")

        except Exception as e:
            flash(f"Couldn’t add machine tab: {e}", "danger")

        return redirect(url_for("index"))

    return app





if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8080)
