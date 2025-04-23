
import serial
import time

def send_file(filepath, port, baudrate, bytesize, parity, stopbits):
    parity_map = {
        "None": serial.PARITY_NONE,
        "Even": serial.PARITY_EVEN,
        "Odd": serial.PARITY_ODD
    }

    with serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity_map[parity],
        stopbits=stopbits,
        timeout=1,
        xonxoff=True
    ) as ser:
        with open(filepath, "r") as file:
            for line in file:
                cleaned = line.strip()
                if cleaned:
                    ser.write((cleaned + "\r\n").encode("ascii"))
                    time.sleep(0.05)
