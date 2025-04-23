
from flask import Flask, render_template, request, redirect, url_for, flash
import os
import json
import time
import serial
import serial.tools.list_ports
from serial_sender import send_file
from datetime import datetime, timedelta
import threading

UPLOAD_FOLDER = "uploads/haas1"
CONFIG_FILE = "serial_config.json"
MACHINE_INFO_FILE = "machine_info.json"
LOG_FILE = "transfer_log.txt"
MONITOR_LOG_FILE = "cnc_monitor_log.txt"


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

serial_lock = threading.Lock()

def load_serial_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {
        "port": "/dev/ttyUSB0",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "N",
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
    if os.path.exists("q600_config.json"):
        with open("q600_config.json", "r") as f:
            return json.load(f)
    return []

def save_q600_config(data):
    with open("q600_config.json", "w") as f:
        json.dump(data, f, indent=4)



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

def log_entry(tag, command, response):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"{timestamp} | {tag} | {command} -> {response}"
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

def monitor_machine(machine_info, serial_config):
    # Update machine info once based on Q-commands
    update_machine_info_from_q_commands(serial_config)

    def monitor_loop():
        while True:
            try:
                with serial_lock:
                    # Auto-configuration if 'auto' is selected for baudrate
                    baudrate = serial_config["baudrate"]
                    if baudrate == "auto":
                        # Try each baud rate in the candidate list until a successful connection
                        for candidate_baudrate in CANDIDATE_BAUD_RATES:
                            print(f"Trying baudrate: {candidate_baudrate}")
                            try:
                                # Attempt connection with each baud rate
                                with serial.Serial(
                                    port=serial_config["port"],
                                    baudrate=candidate_baudrate,
                                    bytesize=8,  # Default databits
                                    parity="N",  # Default parity
                                    stopbits=1,  # Default stopbits
                                    timeout=1,
                                    xonxoff=True
                                ) as ser:
                                    # Test if the serial connection works by sending a basic command
                                    response = send_q_command(ser, TEST_COMMAND)
                                    if response:
                                        print(f" Successfully connected at baudrate {candidate_baudrate}")
                                        # Save the working baud rate
                                        serial_config["baudrate"] = candidate_baudrate
                                        save_serial_config(serial_config)
                                        break
                            except Exception as e:
                                print(f" Failed at baudrate {candidate_baudrate}: {e}")
                        else:
                            print(" No working baudrate found.")
                            continue  # Retry if no baudrate worked
                    else:
                        # Use the manually selected baud rate if it's not 'auto'
                        with serial.Serial(
                            port=serial_config["port"],
                            baudrate=baudrate,
                            bytesize=serial_config["bytesize"],
                            parity=serial_config["parity"],
                            stopbits=serial_config["stopbits"],
                            timeout=1,
                            xonxoff=True
                        ) as ser:
                            # Logging static machine info
                            print("\nLogging static machine information:")
                            for cmd, tag in static_commands.items():
                                result = send_q_command(ser, cmd)
                                log_entry(tag, cmd, result)

                            # Logging dynamic machine info
                            print("\nLogging one cycle of dynamic info:")
                            for cmd, tag in dynamic_commands.items():
                                result = send_q_command(ser, cmd)
                                log_entry(tag, cmd, result)

            except Exception as e:
                print(f"[MONITOR] Failed: {e}")

            time.sleep(machine_info.get("polling_rate", 5))  # Adjust based on polling rate

    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()


def create_app():
    app = Flask(__name__)
    app.secret_key = "cnc-secret-key"
    app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
    app.config["SERIAL_CONFIG"] = load_serial_config()
    app.config["MACHINE_INFO"] = load_machine_info()

    monitor_machine(app.config["MACHINE_INFO"], app.config["SERIAL_CONFIG"])

    @app.route("/", methods=["GET", "POST"])
    def index():
        if request.method == "POST":
            if "file" in request.files:
                file = request.files["file"]
                if file and file.filename.endswith((".nc", ".txt", ".gcode")):
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], file.filename)
                    file.save(filepath)
                    flash(f"Uploaded {file.filename} successfully.", "success")

            if "baudrate" in request.form:
                config = app.config["SERIAL_CONFIG"]
                config["baudrate"] = int(request.form.get("baudrate"))
                config["bytesize"] = int(request.form.get("bytesize"))
                config["parity"] = request.form.get("parity")
                config["stopbits"] = int(request.form.get("stopbits"))
                save_serial_config(config)
                flash("Serial settings updated.", "info")

            if "machine_name" in request.form:
                machine_info = app.config["MACHINE_INFO"]
                machine_info["name"] = request.form.get("machine_name")
                machine_info["manufacturer"] = request.form.get("manufacturer")
                if "polling_rate" in request.form:
                    machine_info["polling_rate"] = int(request.form.get("polling_rate"))
                save_machine_info(machine_info)
                flash("Machine info updated.", "info")

        files = os.listdir(app.config["UPLOAD_FOLDER"])
        log_entries = read_log()
        return render_template("index.html", files=files, config=app.config["SERIAL_CONFIG"], log=log_entries, machine=app.config["MACHINE_INFO"])

    @app.route("/send/<filename>")
    def send(filename):
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        try:
            with serial_lock:
                send_file(filepath, **app.config["SERIAL_CONFIG"])
            update_machine_info_from_q_commands(app.config["SERIAL_CONFIG"])
            flash(f"Sent {filename} to CNC", "success")
            append_log(f"Sent: {filename}")
        except Exception as e:
            flash(f"Failed to send {filename}: {e}", "danger")
            append_log(f"Failed: {filename} - {e}")
        return redirect(url_for("index"))

    @app.route("/variables", methods=["GET", "POST"])
    def variable_config():
        if request.method == "POST":
            selected = request.form.getlist("q600_selected")
            save_q600_config(selected)
            flash("Q600 monitoring variables updated.", "info")
            return redirect(url_for("variable_config"))

        return render_template("variables.html",
            machine=app.config["MACHINE_INFO"],
            q600_variables=q600_variables,
            selected_q600=load_q600_config()
        )
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=8080)
