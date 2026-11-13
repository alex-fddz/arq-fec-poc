#!/usr/bin/env python3
"""
ARQ-FEC Data Collection Orchestrator.
Runs device and server back-to-back for SCENARIO_ITERATIONS rounds,
suppressing per-run output and writing a unified CSV dataset.
"""

import csv
import os
from pathlib import Path
import threading
import time
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime

from config import SCENARIO_ITERATIONS, MAX_ATTEMPTS, MTU_SIZE_BYTES, SCHC_PACKET

CSV_PATH = Path("data")
CSV_FILENAME = CSV_PATH / f'stats_{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}.csv'
CSV_HEADER = [
    "iteration",
    "timestamp",
    "cfg_loss_probability",
    "cfg_allow_all1_loss",
    "dev_regular_count",
    "dev_regular_tx_attempts",
    "dev_regular_lost",
    "dev_all1_lost",
    "dev_c1_recvd",
    "dev_c0_recvd",
    "dev_ackreq_sent",
    "dev_attempts",
    "srv_regular_recvd",
    "srv_all1_recvd",
    "srv_c1_sent",
    "srv_c0_sent",
    "srv_ackreq_recvd",
    "srv_retx_called",
    "srv_decodeable_before_retx",
    "srv_decodeable",
    "srv_rcs_ok",
    "dev_aborted",
    "srv_timed_out",
    "srv_early_ack_sent",
    "cfg_max_attempts",
    "cfg_mtu",
    "cfg_packet_bytes",
]


@contextmanager
def suppress_output():
    with open(os.devnull, "w") as devnull:
        with redirect_stdout(devnull), redirect_stderr(devnull):
            yield


def run_server(ready_event, out):
    from server import main as server_main
    out.append(server_main(ready_event=ready_event))


def run_device(out):
    from device import main as device_main
    out.append(device_main())


def main():
    CSV_PATH.mkdir(parents=True, exist_ok=True)

    file_exists = os.path.isfile(CSV_FILENAME)

    with open(CSV_FILENAME, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)

        print(f"Running {SCENARIO_ITERATIONS} iterations...")
        for i in range(1, SCENARIO_ITERATIONS + 1):
            print(f"  * {i}/{SCENARIO_ITERATIONS}...", end=" ", flush=True)
            t0 = time.perf_counter()

            ready_event = threading.Event()

            server_out: list = []
            device_out: list = []

            server_thread = threading.Thread(
                target=run_server, args=(ready_event, server_out)
            )
            device_thread = threading.Thread(target=run_device, args=(device_out,))

            timed_out = False

            with suppress_output():
                server_thread.start()
                if not ready_event.wait(timeout=10):
                    timed_out = True
                else:
                    device_thread.start()
                    device_thread.join()
                server_thread.join()

            if timed_out:
                print("TIMEOUT (server never ready)")
                return

            elapsed = time.perf_counter() - t0

            device_stats = device_out[0] if device_out else {}
            server_stats = server_out[0] if server_out else {}

            writer.writerow([
                i,
                datetime.now(),
                device_stats.get("loss_probability"),
                device_stats.get("allow_all1_lost"),
                device_stats.get("regular_count"),
                device_stats.get("regular_tx_attempts"),
                device_stats.get("regular_lost"),
                device_stats.get("all1_lost"),
                device_stats.get("c1_recvd"),
                device_stats.get("c0_recvd"),
                device_stats.get("ackreq_sent"),
                device_stats.get("attempts"),
                server_stats.get("regular_recvd"),
                server_stats.get("all1_recvd"),
                server_stats.get("c1_sent"),
                server_stats.get("c0_sent"),
                server_stats.get("ackreq_recvd"),
                server_stats.get("retx_called"),
                server_stats.get("decodeable_before_retx"),
                server_stats.get("decodeable"),
                server_stats.get("rcs_ok"),
                device_stats.get("aborted"),
                server_stats.get("timed_out"),
                server_stats.get("early_ack_sent"),
                MAX_ATTEMPTS,
                MTU_SIZE_BYTES,
                len(SCHC_PACKET),
            ])

            print(f"done ({elapsed:.2f}s)")

        print(f"Statistics saved to {CSV_FILENAME}")


if __name__ == "__main__":
    main()
