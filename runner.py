#!/usr/bin/env python3
"""
ARQ-FEC Data Collection Orchestrator.
Runs device and server back-to-back for SCENARIO_ITERATIONS rounds,
suppressing per-run output and writing a unified CSV dataset.
"""

import csv
import os
import threading
import time
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime

from config import SCENARIO_ITERATIONS

CSV_FILENAME = f'stats_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.csv'
CSV_HEADER = [
    "iteration",
    "timestamp",
    "loss_probability",
    "total_regular",
    "lost_regular",
    "all1_lost",
    "regular_received",
    "all1_received",
    "decodeable",
    "rcs_ok",
]


@contextmanager
def suppress_output():
    with open(os.devnull, "w") as devnull:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            yield


def run_server(ready_event, out):
    from server import main as server_main
    with suppress_output():
        out.append(server_main(ready_event=ready_event))


def run_device(out):
    from device import main as device_main
    with suppress_output():
        out.append(device_main())


def main():
    file_exists = os.path.isfile(CSV_FILENAME)

    with open(CSV_FILENAME, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)

        print(f"Running {SCENARIO_ITERATIONS} iterations...")
        for i in range(1, SCENARIO_ITERATIONS + 1):
            print(f"  Iteration {i}/{SCENARIO_ITERATIONS}...", end=" ", flush=True)
            t0 = time.perf_counter()

            ready_event = threading.Event()

            server_out: list = []
            device_out: list = []

            server_thread = threading.Thread(
                target=run_server, args=(ready_event, server_out)
            )
            server_thread.start()

            if not ready_event.wait(timeout=10):
                print("TIMEOUT (server never ready)")
                return

            device_thread = threading.Thread(target=run_device, args=(device_out,))
            device_thread.start()

            device_thread.join()
            server_thread.join()

            elapsed = time.perf_counter() - t0

            device_stats = device_out[0] if device_out else {}
            server_stats = server_out[0] if server_out else {}

            writer.writerow([
                i,
                datetime.now(),
                device_stats.get("loss_probability"),
                device_stats.get("total_regular"),
                device_stats.get("lost_regular"),
                device_stats.get("all1_lost"),
                server_stats.get("regular_received"),
                server_stats.get("all1_received"),
                server_stats.get("decodeable"),
                server_stats.get("rcs_ok"),
            ])

            print(f"done ({elapsed:.2f}s)")


if __name__ == "__main__":
    main()
