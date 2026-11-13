# ARQ-FEC PoC

Proof of concept for the ARQ-FEC mechanism defined in `draft-munoz-schc-over-dts-iot`. Implements a minimal sender (device) and receiver (server) for each case, with fragmentation over UDP loopback:

| Case | Files | Config |
|---|---|---|
| Stream geometry (XOR-based FEC, tile interleaving) | `device_stream.py`, `server_stream.py`, `runner_stream.py` | `config.py` |
| Matrix geometry (Reed-Solomon FEC) | `device_matrix.py`, `server_matrix.py`, `runner_matrix.py` | `config_matrix.py` |
| No FEC (control case, RFC 8724 Ack-on-Error) | `device_nofec.py`, `server_nofec.py`, `runner_nofec.py` | `config.py` |

## Usage

```
pip install -r requirements.txt
python3 server_stream.py & python3 device_stream.py
```

The device encodes, interleaves, fragments, and transmits. The server listens for fragments, places them into the reception C-Stream, checks decodeability, decodes the SCHC packet, and verifies integrity via the RCS carried in the All-1 fragment. Missing tiles are reported with a SCHC Compound ACK (RFC 9441) and retransmitted, up to `MAX_ATTEMPTS`.

### Batch data collection

```
python3 runner_stream.py
python3 runner_matrix.py
python3 runner_nofec.py
```

Runs the scenario `SCENARIO_ITERATIONS` times, suppressing output and writing one row per run to a timestamped CSV file in `data/` (`stats-stream_*.csv`, `stats-matrix_*.csv`, `stats-nofec_*.csv`). Device and server are synchronized via a `ready_event` so the port is bound before the device sends.

## Analysis

See [analysis.ipynb](./analysis.ipynb) (Jupyter Notebook) for stats and graphs. Set `GEOMETRY` (`"nofec"`, `"stream"` or `"matrix"`) in the first cell to choose which case to analyze.
