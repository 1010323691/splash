#!/usr/bin/env python3
"""Splash monitor: a dashboard for a running Splash server on this Mac.

    python3 monitor/monitor.py

Then open http://127.0.0.1:18081/. Standard library only. It polls the server's
GET /status, follows the log launcher/start.command writes, samples the Mac with
monitor/macos.py (no sudo), and serves the page plus GET /metrics, the numbers
the page draws, and GET /api/status, the last raw status.
"""
import argparse
import collections
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from macos import Hardware

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY = 60                      # one point per second
KEEP_REQUESTS = 500
KEEP_LOG_LINES = 200
REUSE_WINDOW = 100                # requests behind the reuse-path bars and the reuse trend
RECENT_WINDOW = 20                # requests behind "last 20"
RATE_WINDOW_S = 60.0              # cache pressure looks back this far
DECODE_RATE_WINDOW_S = 3.0        # decode tokens arrive in blocks of up to eight
POLL_S = 0.5
ANE_QUIET_S = 8.0                 # the engine's Neural Engine counters move once a prefill block, about 5 s
AUX_MAX_TOKENS = 16               # a request that wrote this few tokens (and used no tools) is tagged "aux"
ZERO_REUSE_ALERT = 3              # consecutive full prefills before the page warns
JOIN_WAIT_S = 3.0                 # how long a finished request waits for its log line
BUCKETS = ((0, 4096, "<4K"), (4096, 16384, "4-16K"), (16384, 65536, "16-64K"), (65536, 1 << 62, ">64K"))
SERIES = ("tok_s", "prefill_tok_s", "gpu_util", "gpu_mem_used", "gpu_power", "temp_soc", "cpu", "ane_active", "disk_read_mb",
          "ram_used", "batch_size", "draft_hit")
# Counters that mean the cache lost something under pressure, by path into /status.
PRESSURE_KEYS = {"state_evictions": ("state", "evictions"), "state_in_use_evictions": ("state", "in_use_evictions"),
                 "checkpoint_evictions": ("state", "checkpoint_evictions"), "kv_demotions": ("disk", "kv_demotions"),
                 "lost_state_misses": ("cache", "lost_state_misses"), "resource_suspensions": ("cache", "resource_suspensions"),
                 "priority_suspensions": ("cache", "priority_suspensions"), "denied_reservations": ("memory_governor", "denied_reservations")}
LATENCY_STAGES = ("http_request", "http_ttft", "upload", "preparation_queue", "preparation", "template", "tokenization",
                  "grammar", "images", "native_queue", "output_interval")
# The memory plan's fixed parts, in the order the page stacks them.
PLAN_PARTS = (("Target weights", "target_weights_bytes"), ("Draft weights", "draft_weights_bytes"), ("Vision", "vision_weights_bytes"),
              ("Prefill", "shared_prefill_bytes"), ("Decode", "shared_decode_bytes"), ("Neural Engine", "ane_ffn_bytes"),
              ("State staging", "state_staging_bytes"), ("Pipelines", "pipeline_reserve_bytes"), ("Runtime", "runtime_overhead_reserve_bytes"),
              ("Streamed table", "streamed_table_bytes"))

LOG_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d) (.*)$")
TOKENS = re.compile(r"^(input|cached|deduplicated|output) ([\d,]+)$")
THROUGH_BAR = re.compile(r"\d+%\|.*\|")   # a download's progress bar


def path_get(data, *keys, default=None):
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def log_time(hours, minutes, seconds):
    """The log's clock time as a Unix time: today, or yesterday once it is in the future."""
    now = time.time()
    today = time.localtime(now)
    at = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, int(hours), int(minutes), int(seconds), 0, 0, -1))
    return at - 86400 if at > now + 60 else at


def parse_request_line(text):
    """A Started, Done or Cancelled line's fields, or None."""
    parts = text.split(" · ")
    if parts[0] not in ("Started", "Done", "Cancelled"):
        return None
    line = {"kind": parts[0], "input": 0, "cached": 0, "deduplicated": 0, "output": 0, "effort": None, "thinking": None,
            "tools": 0, "tool_signature": "", "ttft_s": None, "tok_s": None, "tok_cycle": None}
    for part in parts[1:]:
        token = TOKENS.match(part)
        if token:
            line[token[1]] = int(token[2].replace(",", ""))
        elif part.startswith("TTFT ") and part.endswith("s"):
            line["ttft_s"] = float(part[5:-1])
        elif part.endswith(" tok/s"):
            line["tok_s"] = float(part[:-6])
        elif part.endswith(" tok/cycle"):
            line["tok_cycle"] = float(part[:-10])
        elif part.startswith("effort "):
            line["effort"] = part[7:]
        elif part.startswith("thinking "):
            line["thinking"] = part[9:] == "on"
        elif part.startswith("tools "):
            count, _, signature = part[6:].partition("·")
            line["tools"], line["tool_signature"] = int(count), signature
    return line


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = None                     # the last /status
        self.status_at = 0.0
        self.status_error = "not polled yet"
        self.instance = None
        self.previous = None                   # the status before this one, for counter deltas
        self.previous_at = 0.0
        self.engine_pid = None
        self.engine = None
        self.server_process = None
        self.hw = {}
        self.history = {k: collections.deque(maxlen=HISTORY) for k in SERIES}
        self.history["reuse"] = collections.deque(maxlen=REUSE_WINDOW)         # one point per finished request
        self.history["cache_pressure"] = collections.deque(maxlen=HISTORY)
        self.decode_points = collections.deque()                                # (time, decode tokens so far)
        self.prefill_rate = 0.0                # tokens over the time since the last block finished
        self.prefill_mark = None               # (time, tokens) of the last block
        self.ane_mark = None                   # (time, ane_ms) of the last change
        self.ane_engine_share = None
        self.tracked = {}                      # active request id -> what the polls saw of it
        self.finished = []                     # requests that left the engine, waiting for their log line
        self.started = []                      # Started lines waiting for their Done
        self.done_lines = []                   # Done lines waiting for a tracked request
        self.requests = collections.deque(maxlen=KEEP_REQUESTS)
        self.totals = collections.Counter()
        self.finish_counts = collections.Counter()
        self.error_counts = collections.Counter()
        self.error_recent = collections.deque(maxlen=20)
        self.pressure_total = collections.Counter()
        self.pressure_events = collections.deque()                              # (time, events in that poll)
        self.zero_streak = 0
        self.zero_streak_at = 0.0
        self.last_prompt = None
        self.log_path = ""
        self.log_seen = False
        self.log_lines = collections.deque(maxlen=KEEP_LOG_LINES)
        self.skipped = 0

    # ---- the log
    def apply_line(self, raw):
        text = raw.split("\r")[-1].strip()
        match = LOG_LINE.match(text)
        with self.lock:
            if not match:
                if text and not THROUGH_BAR.search(text) and not text.startswith(("Fetching", "Download complete", "Reconstruction")):
                    self.log_lines.appendleft({"time": None, "text": text[:400]})
                return
            at = log_time(*match.groups()[:3])
            body = match[4]
            line = parse_request_line(body)
            if line is None:
                self.log_lines.appendleft({"time": at, "text": body[:400]})
                if body.startswith("Error · "):
                    parts = body.split(" · ")
                    self._error(at, parts[1] if len(parts) > 1 else "error", " · ".join(parts[2:]) or body)
                return
            line["time"] = at
            if line["kind"] == "Started":
                self.started.append(line)
                del self.started[:-64]
            else:
                self.done_lines.append(line)
                self._join()

    def _error(self, at, code, message):
        self.error_counts[code] += 1
        self.error_recent.appendleft({"time": at, "code": code, "message": message[:200]})
        self.totals["errors"] += 1
        self.requests.appendleft({"id": None, "time": at, "finish": "error", "message": message, "prompt_tokens": 0,
                                  "reused": 0, "deduplicated": 0, "output_tokens": 0, "decode_tok_s": None, "prefill_tok_s": None,
                                  "ttft_s": None, "duration_s": None, "reuse_path": "", "drafted": None, "accepted": None,
                                  "tok_cycle": None, "queue_s": None, "kind": "main", "zero_reuse": False})

    def _join(self):
        """Pair Done lines with the requests the polls tracked, by prompt size."""
        for line in list(self.done_lines):
            match = next((r for r in self.finished if r["prompt_tokens"] == line["input"]), None)
            # A request shorter than a poll was never seen; a live one is not finished yet.
            live = any(r["prompt_tokens"] == line["input"] for r in self.tracked.values())
            if match is None and live:
                continue
            if match is not None:
                self.finished.remove(match)
            self.done_lines.remove(line)
            started = next((s for s in self.started if s["input"] == line["input"]), None)
            if started is not None:
                self.started.remove(started)
            self._record(line, started, match)

    def _record(self, line, started, tracked):
        tracked = tracked or {}
        prompt, output = line["input"] if line else tracked["prompt_tokens"], line["output"] if line else tracked["generated"]
        cached = min(line["cached"], prompt) if line else 0      # the engine counts whole 32-token blocks
        deduplicated = min(line["deduplicated"], prompt - cached) if line else 0
        ttft = line["ttft_s"] if line else None
        at = line["time"] if line else tracked["last_seen"]
        computed = max(0, prompt - cached - deduplicated)
        phases = tracked.get("phases", {})
        begin = phases.get("prefill") or tracked.get("first_seen")
        queue = (phases["prefill"] - tracked["first_seen"]) if "prefill" in phases else None
        prefill_s = (phases["decode"] - phases["prefill"]) if "decode" in phases and "prefill" in phases else None
        decode_s = (tracked["last_seen"] - phases["decode"]) if "decode" in phases else None
        duration = tracked["age_ms"] / 1000 if tracked.get("age_ms") else (at - started["time"] if started else None)
        kind = "cancel" if line and line["kind"] == "Cancelled" else "stop"
        if kind == "stop" and tracked.get("max_new") and output >= tracked["max_new"]:
            kind = "length"
        path = "deduplicated" if deduplicated else "cached prefix" if cached else "cold"
        rec = {"id": tracked.get("id"), "time": at, "finish": kind, "prompt_tokens": prompt, "reused": cached,
               "deduplicated": deduplicated, "output_tokens": output, "decode_tok_s": line["tok_s"] if line else None,
               "prefill_tok_s": computed / ttft if ttft and computed else None, "ttft_s": ttft, "duration_s": duration,
               "reuse_path": path, "drafted": tracked.get("drafted"), "accepted": tracked.get("accepted"),
               "tok_cycle": line["tok_cycle"] if line else None, "queue_s": queue, "prefill_s": prefill_s, "decode_s": decode_s,
               "effort": started["effort"] if started else None, "thinking": started["thinking"] if started else None,
               "tools": line["tools"] if line else 0, "tool_signature": line["tool_signature"] if line else "",
               "kv_mode": tracked.get("kv_mode"), "priority": tracked.get("priority"),
               "kind": "aux" if output <= AUX_MAX_TOKENS and not (line and line["tools"]) else "main", "zero_reuse": False,
               "source": "log" if line else "status"}
        if cached or deduplicated:
            self.zero_streak = 0
        elif prompt and self.last_prompt is not None and prompt > self.last_prompt:
            self.zero_streak += 1
            self.zero_streak_at = at
            rec["zero_reuse"] = True
        self.last_prompt = prompt
        if prompt:
            self.history["reuse"].append((cached + deduplicated) / prompt)
        self.finish_counts[kind] += 1
        self.requests.appendleft(rec)
        t = self.totals
        t["requests"] += 1
        t["prompt_tokens"] += prompt
        t["reused"] += cached + deduplicated
        t["computed"] += computed
        t["output_tokens"] += output
        t["prefill_s"] += ttft if ttft and computed else 0
        t["decode_s"] += output / line["tok_s"] if line and line["tok_s"] else 0
        if t["since"] == 0:
            t["since"] = at

    # ---- /status
    def apply_status(self, status, now):
        with self.lock:
            identity = path_get(status, "instance", "id")
            if identity != self.instance:             # a restarted server's counters start again
                self.instance = identity
                self.previous, self.decode_points, self.prefill_mark, self.ane_mark = None, collections.deque(), None, None
                self.prefill_rate, self.ane_engine_share = 0.0, None
                self.finished.extend(self._closing(r) for r in self.tracked.values())
                self.tracked = {}
                self.zero_streak, self.zero_streak_at, self.last_prompt = 0, 0.0, None
            self._track(status, now)
            self._rates(status, now)
            self._pressure(status, now)
            self.status, self.status_at, self.status_error = status, now, None
            self.previous, self.previous_at = status, now
            self._expire(now)

    @staticmethod
    def _closing(record):
        return dict(record, left=time.time())

    def _track(self, status, now):
        active = status.get("active_requests") or []
        previous = self.previous or status
        change = {key: path_get(status, "metrics", key, default=0) - path_get(previous, "metrics", key, default=0)
                  for key in ("drafted_tokens", "accepted_draft_tokens")}
        seen = {}
        for item in active:
            rid = item.get("id")
            record = self.tracked.get(rid)
            if record is None:
                record = {"id": rid, "first_seen": now - (item.get("age_ms") or 0) / 1000, "phases": {}, "drafted": 0, "accepted": 0,
                          "solo": True, "prompt_tokens": item.get("prompt_tokens", 0), "max_new": item.get("max_new_tokens")}
            record["phases"].setdefault(item.get("phase"), now)
            record.update(prompt_tokens=item.get("prompt_tokens", 0), processed=item.get("prompt_processed", 0),
                          generated=item.get("generated_tokens", 0), age_ms=item.get("age_ms"), last_seen=now,
                          phase=item.get("phase"), kv_mode=item.get("kv_mode"), priority=item.get("priority"))
            seen[rid] = record
        # Draft counters are the engine's: they name a request only while it runs alone.
        running = [r for r in self.tracked.values() if r.get("phase") == "decode"] or list(seen.values())
        if len(self.tracked) > 1:
            for record in self.tracked.values():
                record["solo"] = False
        if len(running) == 1:
            running[0]["drafted"] += max(0, change["drafted_tokens"])
            running[0]["accepted"] += max(0, change["accepted_draft_tokens"])
        for rid, record in self.tracked.items():
            if rid not in seen:
                if not record["solo"]:
                    record["drafted"], record["accepted"] = None, None
                self.finished.append(self._closing(record))
        self.tracked = seen
        self._join()

    def _rates(self, status, now):
        metrics = status.get("metrics") or {}
        decoded = metrics.get("decode_output_tokens", 0)
        points = self.decode_points
        points.append((now, decoded))
        while len(points) > 2 and now - points[0][0] > DECODE_RATE_WINDOW_S:
            points.popleft()
        prefilled = metrics.get("prefill_input_tokens", 0)
        prefilling = any(r.get("phase") == "prefill" for r in self.tracked.values())
        if self.prefill_mark is None or not prefilling and self.prefill_mark[1] == prefilled:
            self.prefill_mark = (now, prefilled)
        elif prefilled != self.prefill_mark[1]:         # a block of prefill rows finished
            seconds = now - self.prefill_mark[0]
            self.prefill_rate = (prefilled - self.prefill_mark[1]) / seconds if seconds > 0 else 0.0
            self.prefill_mark = (now, prefilled)
        ane_ms = path_get(status, "ane_ffn", "ane_ms", default=0)
        if self.ane_mark is None:
            self.ane_mark = (now, ane_ms)
        elif ane_ms != self.ane_mark[1]:
            seconds = now - self.ane_mark[0]
            self.ane_engine_share = min(1.0, (ane_ms - self.ane_mark[1]) / 1000 / seconds) if seconds > 0 else None
            self.ane_mark = (now, ane_ms)
        elif not prefilling:
            self.ane_engine_share = None

    def _pressure(self, status, now):
        previous = self.previous
        if previous is None:
            return
        events = 0
        for name, path in PRESSURE_KEYS.items():
            change = path_get(status, *path, default=0) - path_get(previous, *path, default=0)
            if change > 0:
                self.pressure_total[name] += change
                events += change
        self.pressure_events.append((now, events))
        while self.pressure_events and self.pressure_events[0][0] < now - RATE_WINDOW_S:
            self.pressure_events.popleft()

    def _expire(self, now):
        for record in list(self.finished):
            if now - record["left"] > JOIN_WAIT_S:     # no log, or no line for it: show what the polls saw
                self.finished.remove(record)
                self._record(None, None, record)
        del self.done_lines[:-64]

    def decode_rate(self):
        points = self.decode_points
        if len(points) < 2 or points[-1][0] <= points[0][0]:
            return 0.0
        return max(0.0, (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0]))

    @staticmethod
    def working(status):
        """Whether any request is decoding, and whether any is prefilling."""
        phases = [item.get("phase") for item in status.get("active_requests") or []]
        return "decode" in phases, "prefill" in phases

    def state_now(self):
        status, now = self.status, time.time()
        if status is None or now - self.status_at > 3 * POLL_S + 1:
            return "offline"
        if path_get(status, "transport", "recovering"):
            return "recovering"
        if path_get(status, "transport", "stopped") or path_get(status, "metal", "healthy") is False:
            return "error"
        phases = [item.get("phase") for item in status.get("active_requests") or []]
        if path_get(status, "weights", "restoring"):
            return "restoring"
        if "prefill" in phases:
            return "reading"
        if "decode" in phases:
            return "generating"
        if phases:
            return "queued"
        return "released" if path_get(status, "weights", "released") else "idle"

    # ---- a sample of the history, once a second
    def sample(self, hw):
        with self.lock:
            self.hw = hw
            status = self.status or {}
            decoding, prefilling = self.working(status)
            # The SoC counts the Neural Engine as active while it is awake, which
            # outlasts its work; only the engine's own counters say it is working.
            hw = dict(hw, ane_awake=hw.get("ane_active"))
            if self.ane_mark is None or time.time() - self.ane_mark[0] > ANE_QUIET_S:
                hw["ane_active"] = 0.0
            self.hw = hw
            batch = path_get(status, "metrics", "current_decode_batch", "width", default=0) if decoding else 0
            hits = path_get(status, "metrics", "current_decode_batch", default={})
            drafted = hits.get("drafted_tokens") or 0
            row = dict(hw, tok_s=self.decode_rate() if decoding else 0, prefill_tok_s=self.prefill_rate if prefilling else 0,
                       gpu_mem_used=path_get(status, "memory_actual", "current_bytes"), batch_size=batch,
                       draft_hit=(hits.get("accepted_draft_tokens", 0) / drafted) if drafted and decoding else None)
            for key in SERIES:
                self.history[key].append(row.get(key))
            self.history["cache_pressure"].append(sum(v for t, v in self.pressure_events if t >= time.time() - 1))

    # ---- the numbers the page draws
    def metrics(self, show_all):
        now = time.time()
        with self.lock:
            s = self.status or {}
            state = self.state_now()
            online = state != "offline"
            budget = path_get(s, "memory_plan", "budget", default={})
            model = path_get(s, "memory_plan", "model", default={})
            device = path_get(s, "memory_plan", "device", default={})
            mt = s.get("metrics") or {}
            sch = s.get("scheduler") or {}
            active = []
            for item in (s.get("active_requests") or []) if online else []:
                record = self.tracked.get(item.get("id")) or {}
                active.append(dict(item, drafted=record.get("drafted"), accepted=record.get("accepted")))
            waiting = sum(sch.get(k, 0) for k in ("queued", "waiting_resources", "waiting_prefix", "waiting_mask"))
            decoding, prefilling = self.working(s) if online else (False, False)
            live = {"state": state, "queued": waiting, "running": sch.get("prefilling", 0) + sch.get("decoding", 0),
                    "prefilling": sch.get("prefilling", 0), "decoding": sch.get("decoding", 0),
                    "tok_s": self.decode_rate() if decoding else 0, "prefill_tok_s": self.prefill_rate if prefilling else 0,
                    "active": active, "weights": s.get("weights") or {}, "started_at": path_get(s, "instance", "started_at"),
                    "admission": s.get("admission") or {}}
            recent = list(self.requests)
            shown = recent if show_all else recent[:12]
            tot = dict(self.totals)
            done = [r for r in recent if r["finish"] != "error"]
            window = done[:RECENT_WINDOW]
            prompts = sum(r["prompt_tokens"] for r in window)
            reused = sum(r["reused"] + r["deduplicated"] for r in window)
            paths = {}
            for r in done[:REUSE_WINDOW]:
                path = paths.setdefault(r["reuse_path"] or "none", {"n": 0, "tokens": 0})
                path["n"] += 1
                path["tokens"] += r["reused"] + r["deduplicated"]
            buckets = []
            for low, high, name in BUCKETS:
                rows = [r["prefill_tok_s"] for r in done if low <= r["prompt_tokens"] < high and r["prefill_tok_s"]]
                buckets.append({"name": name, "n": len(rows), "avg_tok_s": sum(rows) / len(rows) if rows else None})
            pressure = {"total": dict(self.pressure_total), "last60s": sum(v for t, v in self.pressure_events if t >= now - RATE_WINDOW_S),
                        "series": list(self.history["cache_pressure"])}
            width = path_get(s, "scheduler", "decode_batches_by_width", default={})
            cycles = sch.get("decode_batches", 0)
            timing = s.get("model_timing") or {}
            decode_wall, decode_cycle = mt.get("decode_wall_ms"), mt.get("decode_cycle_ms")
            prefill_wall = path_get(timing, "prefill", "total_wall_ms")
            ane_ms = path_get(s, "ane_ffn", "ane_ms")
            perf = {"decode_gpu_share": decode_wall / decode_cycle if decode_wall and decode_cycle else None,
                    "decode_host_share": 1 - decode_wall / decode_cycle if decode_wall and decode_cycle else None,
                    "prefill_ane_share": min(1.0, ane_ms / prefill_wall) if ane_ms and prefill_wall else None,
                    "decode_wall_ms": decode_wall, "decode_cycle_ms": decode_cycle, "prefill_wall_ms": prefill_wall,
                    "last_prefill_ms": path_get(timing, "prefill", "last_wall_ms"), "last_decode_ms": path_get(timing, "decode", "last_wall_ms")}
            per_request = [{"tok_cycle": r["tok_cycle"], "hit": r["accepted"] / r["drafted"] if r.get("drafted") else None}
                           for r in done[:RECENT_WINDOW] if r.get("tok_cycle")][::-1]
            speculative = {"drafted": mt.get("drafted_tokens"), "accepted": mt.get("accepted_draft_tokens"), "cycles": cycles,
                           "rate": mt.get("draft_acceptance_rate"), "verify_rows": s.get("verify_rows"),
                           "output": mt.get("decode_output_tokens"), "recent": per_request,
                           "block": 8, "resets": path_get(s, "draft_context", "resets")}
            errors = {"total": tot.get("errors", 0), "by_reason": [[k, v] for k, v in self.error_counts.most_common()],
                      "recent": list(self.error_recent),
                      "counters": {"requests failed": path_get(s, "requests", "failed", default=0),
                                   "requests cancelled": path_get(s, "requests", "cancelled", default=0),
                                   "capacity failures": mt.get("capacity_failures", 0), "Metal failures": mt.get("metal_failures", 0),
                                   "engine restarts": path_get(s, "transport", "restarts", default=0),
                                   "weight restore failures": path_get(s, "weights", "restore_failures", default=0),
                                   "Neural Engine reruns": path_get(s, "ane_ffn", "reruns", default=0)},
                      "transport_error": path_get(s, "transport", "error"), "metal_reason": path_get(s, "metal", "failure_reason")}
            latency = {}
            for stage in LATENCY_STAGES:
                entry = path_get(s, "latency", stage, default={})
                count = entry.get("count") or 0
                latency[stage] = {"count": count, "mean_s": entry.get("sum", 0) / count if count else None, "buckets": entry.get("buckets") or {}}
            kv, st, disk = s.get("kv") or {}, s.get("state") or {}, s.get("disk") or {}
            cache = dict(s.get("cache") or {}, kv_pages_used=(kv.get("pages_active") or 0) + (kv.get("pages_cache") or 0),
                         kv_pages_active=kv.get("pages_active"), kv_pages_cache=kv.get("pages_cache"), kv_pages_allocated=kv.get("pages_allocated"),
                         kv_pages_total=budget.get("kv_capacity_pages"), kv_page_tokens=budget.get("kv_page_tokens"),
                         kv_allocated=kv.get("allocated_bytes"), kv_capacity=budget.get("kv_capacity_bytes"), kv_capacity_tokens=budget.get("kv_capacity_tokens"),
                         kv_extents=kv.get("extent_allocations"), kv_compactions=kv.get("extent_compactions"),
                         state=st, disk=disk, kv_window=kv.get("window"), kv_spill=s.get("kv_spill"),
                         images=s.get("images"), image_cache=s.get("image_cache"), tokenizer=s.get("tokenizer_cache"),
                         grammar=s.get("grammar_cache"), responses=s.get("response_store"))
            governor = s.get("memory_governor") or {}
            memory = {"current": path_get(s, "memory_actual", "current_bytes"), "peak": path_get(s, "memory_actual", "peak_bytes"),
                      "limit": governor.get("limit_bytes") or budget.get("hard_budget_bytes"), "headroom": governor.get("headroom_bytes"),
                      "fixed": budget.get("fixed_runtime_bytes"), "dynamic": budget.get("dynamic_budget_bytes"),
                      "parts": [[name, budget.get(key) or 0] for name, key in PLAN_PARTS if budget.get(key)],
                      "kv_allocated": kv.get("allocated_bytes"), "kv_capacity": budget.get("kv_capacity_bytes"),
                      "lane_state": budget.get("lane_state_bytes"), "lanes": budget.get("maximum_batch_width"),
                      "host_available": governor.get("host_available_bytes"), "host_reserve": governor.get("host_reserve_bytes"),
                      "growth_allowed": governor.get("growth_allowed"), "pressure": s.get("memory_pressure"),
                      "context_bound": path_get(budget, "context_bounds", "bound"), "recommended": budget.get("recommended_working_set_bytes"),
                      "configured": budget.get("configured_memory_limit_bytes")}
            http = s.get("http") or {}
            return {
                "engine": {"model": path_get(s, "instance", "model"), "max_context": s.get("maximum_context_tokens"),
                           "lanes": budget.get("maximum_batch_width"), "kv_format": model.get("kv_format"), "state_format": model.get("state_format"),
                           "kv_capacity_tokens": budget.get("kv_capacity_tokens"), "vision": s.get("vision"), "modalities": s.get("input_modalities"),
                           "instance": self.instance, "pid": path_get(s, "instance", "pid"), "port": path_get(s, "instance", "port"),
                           "started_at": path_get(s, "instance", "started_at"), "device": device, "thermal": s.get("thermal_state"),
                           "ane": s.get("ane_ffn"), "ane_engine_share": self.ane_engine_share,
                           "ready": s.get("ready"), "layers": model.get("attention_layers"), "reasoning_switch": s.get("reasoning_switch"),
                           "chat_template": s.get("chat_template"), "warmup": s.get("warmup"), "streamed_table": s.get("streamed_table"), "streamed_experts": s.get("streamed_experts")},
                "live": live, "cache": cache, "memory": memory,
                "reuse": {"recent": reused / prompts if prompts else None, "paths": paths, "window": min(REUSE_WINDOW, len(done)),
                          "zero_streak": self.zero_streak, "streak_age_s": now - self.zero_streak_at if self.zero_streak_at else None,
                          "alert_at": ZERO_REUSE_ALERT, "zero_of": [sum(1 for r in window if r["prompt_tokens"] and not (r["reused"] or r["deduplicated"])), len(window)]},
                "perf": perf, "prefill_buckets": buckets, "pressure": pressure, "speculative": speculative,
                "batch": {"width": path_get(mt, "current_decode_batch", "width") if decoding else 0, "lanes": budget.get("maximum_batch_width"),
                          "running": live["running"], "waiting": waiting, "by_width": width, "cycles": cycles},
                "errors": errors, "finish_counts": dict(self.finish_counts), "config": {"aux_max_tokens": AUX_MAX_TOKENS},
                "latency": latency, "ttft": mt.get("ttft_ms"), "itl": mt.get("itl_ms"),
                "http": http, "frontend": s.get("frontend"), "transport": s.get("transport"), "loop": s.get("loop"),
                "requests_counters": s.get("requests"),
                "scheduler": sch, "draft_context": s.get("draft_context"),
                "hardware": dict(self.hw), "hardware_static": HARDWARE.static, "hardware_errors": HARDWARE.errors,
                "process": {"server": self.server_process, "engine": self.engine},
                "history": {k: list(v) for k, v in self.history.items()},
                "requests": shown, "kept": len(recent), "totals": tot,
                "log": {"path": self.log_path, "found": self.log_seen, "skipped": self.skipped, "lines": list(self.log_lines)[:KEEP_LOG_LINES]},
                "server": {"url": SERVER_URL, "online": online, "error": self.status_error},
                "now": now}


STATE = State()
HARDWARE = None
SERVER_URL = ""
SERVER_KEY = ""


def fetch_status():
    request = urllib.request.Request(SERVER_URL + "/status")
    if SERVER_KEY:
        request.add_header("Authorization", f"Bearer {SERVER_KEY}")
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def poll_status():
    """Poll the server twice a second; a failed poll leaves the last status to go stale."""
    while True:
        started = time.time()
        try:
            STATE.apply_status(fetch_status(), started)
        except (OSError, ValueError, urllib.error.URLError) as error:
            with STATE.lock:
                STATE.status_error = str(getattr(error, "reason", error))
        except Exception as error:         # one odd status must not stop the monitor
            with STATE.lock:
                STATE.skipped += 1
                STATE.status_error = f"{type(error).__name__}: {error}"
        time.sleep(max(0.05, POLL_S - (time.time() - started)))


def tail_log(path):
    """Follow the log: read what is there, then new lines; start over if it is replaced."""
    STATE.log_path = path
    f, inode, position, buffer = None, None, 0, b""
    while True:
        try:
            info = os.stat(path)
            if f is None or info.st_ino != inode or info.st_size < position:
                if f:
                    f.close()
                f, inode, position, buffer = open(path, "rb"), info.st_ino, 0, b""
                STATE.log_seen = True
            chunk = f.read(1 << 20)
            if chunk:
                position += len(chunk)
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if line.strip():
                        try:
                            STATE.apply_line(line.decode("utf-8", "replace"))
                        except Exception:          # one odd line must not stop the follower
                            STATE.skipped += 1
                continue
        except FileNotFoundError:
            STATE.log_seen = False
            f = None
        time.sleep(0.25)


def engine_child(server_pid):
    """The native engine process under the server, or None."""
    try:
        out = subprocess.run(["/usr/bin/pgrep", "-P", str(server_pid), "-f", "serve-native"], capture_output=True, text=True, timeout=2).stdout
        return int(out.split()[0]) if out.split() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def sampler():
    HARDWARE.sample()
    while True:
        started = time.time()
        hw = HARDWARE.sample()
        with STATE.lock:
            pid = path_get(STATE.status, "instance", "pid")
            engine_pid = STATE.engine_pid
        if pid and (engine_pid is None or HARDWARE.process(engine_pid) is None):
            engine_pid = engine_child(pid)
        server = HARDWARE.process(pid) if pid else None
        engine = HARDWARE.process(engine_pid) if engine_pid else None
        with STATE.lock:
            STATE.engine_pid, STATE.server_process, STATE.engine = engine_pid, server, engine
        STATE.sample(hw)
        time.sleep(max(0.05, 1.0 - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    api_key = ""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path in ("/metrics", "/api/metrics", "/api/status"):
            if self.api_key and self.headers.get("Authorization") != f"Bearer {self.api_key}":
                return self._send(401, b'{"error":"API key needed"}', "application/json")
            if path == "/api/status":
                with STATE.lock:
                    body = json.dumps(STATE.status, default=str).encode()
            else:
                body = json.dumps(STATE.metrics("requests=all" in (url.query or "")), default=str).encode()
            return self._send(200, body, "application/json")
        relative = "index.html" if path in ("/", "/index.html") else os.path.normpath(path.lstrip("/"))
        if relative.startswith(("..", "/")):
            return self._send(404, b"not found", "text/plain")
        # the page is served from web/ at the site root; fonts/ stays its own folder (tokens.css asks for ../fonts/)
        full = next((p for p in (os.path.join(HERE, "web", relative), os.path.join(HERE, relative) if relative.startswith("fonts") else "")
                     if p and os.path.isfile(p)), None)
        if full is None:
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def main():
    global HARDWARE, SERVER_URL, SERVER_KEY
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="the Splash server to watch")
    parser.add_argument("--server-key", default=os.environ.get("SPLASH_API_KEY", ""), help="the server's --api-key, if it has one")
    parser.add_argument("--log", help="the server's log (default: ~/Library/Logs/Splash/serve-<port>.log, as launcher/start.command writes it)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--api-key", default="", help="require 'Authorization: Bearer KEY' on /metrics and /api/status")
    args = parser.parse_args()
    SERVER_URL = args.server.rstrip("/")
    SERVER_KEY = args.server_key
    HARDWARE = Hardware()
    Handler.api_key = args.api_key
    port = urlparse(SERVER_URL).port or 8000
    log = args.log or os.path.expanduser(f"~/Library/Logs/Splash/serve-{port}.log")
    threading.Thread(target=tail_log, args=(os.path.abspath(log),), daemon=True).start()
    threading.Thread(target=poll_status, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Splash monitor: http://{args.host}:{args.port}/  (server: {SERVER_URL}, log: {os.path.abspath(log)})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
