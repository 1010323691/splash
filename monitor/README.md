# Splash monitor

A dashboard for a running Splash server, laid out like ninfer's monitor: model
state, speed, memory, context fill, recent requests, the cache, and the Mac's
GPU, Neural Engine, CPU, temperature and disk. It adds what Splash reports and
ninfer's log does not: each request's live progress, the memory plan, the Neural
Engine split, the SSD cache, latency by stage, and every field of `/status`.

The style resources (`web/tokens.css`, `components.css`, `sprite.svg`) come from
ninfer's monitor, which copied them from the Strata web UI, whose tokens derive
from the Gainsty design system; `fonts/` is Outfit under the SIL OFL
(`fonts/OFL.txt`).

## Use

Double-click `launcher/monitor.command`, or:

    python3 monitor/monitor.py

Then open <http://127.0.0.1:18081/>. `launcher/monitor-stop.command` stops it;
the server keeps running, and the monitor waits for a server that is not up.

Options: `--server` (`http://127.0.0.1:8000`), `--server-key` (the server's
`--api-key`, also `SPLASH_API_KEY`), `--log`, `--port` (18081), `--host`
(127.0.0.1), `--api-key KEY` (`/metrics` and `/api/status` then need
`Authorization: Bearer KEY`). Do not bind it beyond 127.0.0.1 without a key.

It needs Python 3 and nothing else, and it never needs sudo.

## Data

| Data | Source |
| --- | --- |
| state, progress, phases, queue | `/status` `active_requests`, `scheduler`, twice a second |
| speeds | deltas of `metrics.decode_output_tokens` and `prefill_input_tokens` (prefill moves in 2048-token blocks) |
| memory plan, headroom, pressure | `memory_plan`, `memory_actual`, `memory_governor` |
| cache, SSD, Neural Engine, latency, errors | their `/status` sections |
| finished requests: tokens, cached, TTFT, tok/s, tok/cycle, tools, effort | the Started and Done lines of `~/Library/Logs/Splash/serve-<port>.log` |
| a request's draft hit, queue / prefill / decode split | the polls, while it ran |
| GPU load, Metal memory in use | I/O Registry `AGXAccelerator` `PerformanceStatistics` |
| GPU power, frequency; CPU clusters; Neural Engine awake | IOReport (`libIOReport`) |
| temperatures | the HID event system's sensors |
| CPU, RAM, swap | Mach and sysctl |
| disk | I/O Registry `IOBlockStorageDriver` |
| each process's footprint, CPU, disk | `proc_pid_rusage` |

`monitor/macos.py` holds the Mac's sources; run it alone to print what this Mac
gives. A source a macOS release removes leaves its fields blank, and its reason
in `hardware_errors` in `/metrics`.

## Limits

- **Neural Engine:** the SoC's counters read active while the Neural Engine is
  awake, which lasts past its work. The card shows that activity only while the
  engine's own `ane_ffn` counters moved in the last 8 s, and "awake" otherwise.
- **Power:** the Macs checked report GPU energy alone: no CPU, Neural Engine or
  DRAM rail, so there is no total. The figure is a 3 s mean, since the counter
  arrives in lumps.
- **Draft hit:** the engine's counters are global, so a request gets a figure only
  while it ran alone. There are no per-position counts, so the chart shows tokens
  per cycle by request instead of ninfer's acceptance by position.
- **Finish reason:** the log does not say why a request ended. "Max tokens" means
  its output reached its `max_new_tokens`.
- **Times:** queue, prefill and decode splits have the polls' 0.5 s grain. A request
  shorter than a poll has only its log line. Prefill speed by prompt size is the
  tokens read over TTFT.
- **Pairing:** a Done line is paired with its request by prompt size, so two
  concurrent requests of one size can swap their figures.
- **History:** 60 s of samples, in memory; requests are the newest 500 seen since
  the monitor started, and the log is replayed at its start. `start.command` empties
  the log, so a monitor started after a restart sees only that run's requests.
- Without the log (a server started by hand), rows hold what the polls saw.
