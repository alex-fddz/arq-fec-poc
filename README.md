# ARQ-FEC PoC

Proof of concept for the Stream encoding geometry profile defined in `draft-munoz-schc-over-dts-iot`. Implements a minimal sender (device) and receiver (server) with XOR-based FEC, tile interleaving, and fragmentation over UDP loopback.

## Usage

```
python3 server.py & python3 device.py
```

The device encodes, interleaves, fragments, and transmits. The server listens for fragments, places them into the reception C-Stream, checks decodeability, decodes the SCHC packet, and verifies integrity via the RCS carried in the All-1 fragment.

Parameters are in `config.py`.
