"""What the Mac reports about itself without sudo, for monitor.py.

IOReport gives the GPU's energy and P-states, the CPU cores' states and the
Neural Engine's idle counters; the HID event system gives the temperature
sensors; Mach and sysctl give CPU ticks, memory and swap; the I/O Registry gives
the GPU's utilization and the disk's byte counters. Every source is optional:
one a macOS release changes or removes leaves its fields out of sample().
Nothing here needs a third-party package.
"""
import collections
import ctypes as C
import os
import plistlib
import subprocess
import time

V = C.c_void_p
UTF8 = 0x08000100
TICKS_PER_SECOND = 24_000_000      # IOReport's 24Mticks unit
HOST_VM_INFO64, HOST_CPU_LOAD_INFO = 4, 3
RUSAGE_INFO_V4 = 4
POWER_WINDOW = 3                   # seconds the GPU power is averaged over
SYSTEM = "/System/Library/Frameworks/"


def _bind(lib, name, restype, *argtypes):
    function = getattr(lib, name)
    function.restype, function.argtypes = restype, list(argtypes)
    return function


class CoreFoundation:
    def __init__(self):
        cf = C.CDLL(SYSTEM + "CoreFoundation.framework/CoreFoundation")
        self.lib = cf
        self.create_string = _bind(cf, "CFStringCreateWithCString", V, V, C.c_char_p, C.c_uint32)
        self.get_string = _bind(cf, "CFStringGetCString", C.c_bool, V, C.c_char_p, C.c_long, C.c_uint32)
        self.release = _bind(cf, "CFRelease", None, V)
        self.count = _bind(cf, "CFArrayGetCount", C.c_long, V)
        self.at = _bind(cf, "CFArrayGetValueAtIndex", V, V, C.c_long)
        self.value = _bind(cf, "CFDictionaryGetValue", V, V, V)
        self.mutable_copy = _bind(cf, "CFDictionaryCreateMutableCopy", V, V, C.c_long, V)
        self.number = _bind(cf, "CFNumberCreate", V, V, C.c_long, V)
        self.dictionary = _bind(cf, "CFDictionaryCreate", V, V, V, V, C.c_long, V, V)
        self.strings = {}

    def s(self, text):
        """A CFString that lives as long as the process."""
        if text not in self.strings:
            self.strings[text] = self.create_string(None, text.encode(), UTF8)
        return self.strings[text]

    def text(self, ref):
        if not ref:
            return None
        buffer = C.create_string_buffer(256)
        return buffer.value.decode() if self.get_string(ref, buffer, 256, UTF8) else None


class IOReport:
    """Deltas between two samples of the channels this page draws."""

    GROUPS = (("Energy Model", None), ("GPU Stats", "GPU Performance States"),
              ("CPU Stats", "CPU Core Performance States"), ("SoC Stats", "PMGR Counters"))

    def __init__(self, cf):
        self.cf = cf
        io = C.CDLL("/usr/lib/libIOReport.dylib")
        self.copy_group = _bind(io, "IOReportCopyChannelsInGroup", V, V, V, C.c_uint64, C.c_uint64, C.c_uint64)
        self.merge = _bind(io, "IOReportMergeChannels", None, V, V, V)
        self.subscribe = _bind(io, "IOReportCreateSubscription", V, V, V, C.POINTER(V), C.c_uint64, V)
        self.samples = _bind(io, "IOReportCreateSamples", V, V, V, V)
        self.delta = _bind(io, "IOReportCreateSamplesDelta", V, V, V, V)
        self.group = _bind(io, "IOReportChannelGetGroup", V, V)
        self.name = _bind(io, "IOReportChannelGetChannelName", V, V)
        self.unit = _bind(io, "IOReportChannelGetUnitLabel", V, V)
        self.integer = _bind(io, "IOReportSimpleGetIntegerValue", C.c_int64, V, V)
        self.state_count = _bind(io, "IOReportStateGetCount", C.c_int32, V)
        self.state_name = _bind(io, "IOReportStateGetNameForIndex", V, V, C.c_int32)
        self.residency = _bind(io, "IOReportStateGetResidency", C.c_int64, V, C.c_int32)
        channels = None
        for group, subgroup in self.GROUPS:
            found = self.copy_group(cf.s(group), cf.s(subgroup) if subgroup else None, 0, 0, 0)
            if not found:
                continue
            if channels is None:
                channels = found
            else:
                self.merge(channels, found, None)
                cf.release(found)
        if channels is None:
            raise OSError("no IOReport channels")
        desired = cf.mutable_copy(None, 0, channels)
        self.subscribed = V()
        self.handle = self.subscribe(None, desired, C.byref(self.subscribed), 0, None)
        if not self.handle:
            raise OSError("no IOReport subscription")
        self.previous = self.samples(self.handle, self.subscribed, None)
        self.previous_at = time.monotonic()

    def _states(self, channel):
        return {self.cf.text(self.state_name(channel, i)): self.residency(channel, i)
                for i in range(self.state_count(channel))}

    def read(self):
        """(seconds covered, {"gpu_joules", "gpu_states", "cores", "ane_idle"})."""
        current, now = self.samples(self.handle, self.subscribed, None), time.monotonic()
        change = self.delta(self.previous, current, None)
        elapsed = now - self.previous_at
        self.cf.release(self.previous)
        self.previous, self.previous_at = current, now
        out = {"gpu_joules": None, "gpu_states": {}, "cores": {}, "ane_idle": {}}
        channels = self.cf.value(change, self.cf.s("IOReportChannels"))
        scale = {"mJ": 1e-3, "uJ": 1e-6, "nJ": 1e-9}
        for i in range(self.cf.count(channels) if channels else 0):
            channel = self.cf.at(channels, i)
            group, name = self.cf.text(self.group(channel)), self.cf.text(self.name(channel))
            if group == "Energy Model" and name == "GPU Energy":
                unit = (self.cf.text(self.unit(channel)) or "").strip()
                out["gpu_joules"] = self.integer(channel, None) * scale.get(unit, 0)
            elif group == "GPU Stats" and name == "GPUPH":
                out["gpu_states"] = self._states(channel)
            elif group == "CPU Stats" and name and "CPU" in name:
                out["cores"][name] = self._states(channel)
            elif group == "SoC Stats" and name and name.startswith("ANE") and name.endswith("_SYS_IDLE"):
                out["ane_idle"][name] = self.integer(channel, None)
        self.cf.release(change)
        return elapsed, out


class Temperatures:
    """The HID event system's temperature sensors, by name, in Celsius."""

    EVENT_TEMPERATURE = 15

    def __init__(self, cf):
        self.cf = cf
        kit = C.CDLL(SYSTEM + "IOKit.framework/IOKit")
        create = _bind(kit, "IOHIDEventSystemClientCreate", V, V)
        matching = _bind(kit, "IOHIDEventSystemClientSetMatching", C.c_int32, V, V)
        self.services = _bind(kit, "IOHIDEventSystemClientCopyServices", V, V)
        self.prop = _bind(kit, "IOHIDServiceClientCopyProperty", V, V, V)
        self.event = _bind(kit, "IOHIDServiceClientCopyEvent", V, V, C.c_int64, C.c_int32, C.c_int64)
        self.float_value = _bind(kit, "IOHIDEventGetFloatValue", C.c_double, V, C.c_int32)

        def number(value):
            box = C.c_int32(value)
            return cf.number(None, 3, C.byref(box))

        keys = (V * 2)(cf.s("PrimaryUsagePage"), cf.s("PrimaryUsage"))
        values = (V * 2)(number(0xff00), number(5))
        key_callbacks = C.addressof(C.c_char.in_dll(cf.lib, "kCFTypeDictionaryKeyCallBacks"))
        value_callbacks = C.addressof(C.c_char.in_dll(cf.lib, "kCFTypeDictionaryValueCallBacks"))
        self.client = create(None)
        matching(self.client, cf.dictionary(None, keys, values, 2, key_callbacks, value_callbacks))

    def read(self):
        services = self.services(self.client)
        out = {}
        for i in range(self.cf.count(services) if services else 0):
            service = self.cf.at(services, i)
            ref = self.prop(service, self.cf.s("Product"))
            name = self.cf.text(ref)
            event = self.event(service, self.EVENT_TEMPERATURE, 0, 0)
            if ref:
                self.cf.release(ref)
            if event:
                value = self.float_value(event, self.EVENT_TEMPERATURE << 16)
                self.cf.release(event)
                if name and 0 < value < 150:     # a sensor that is not wired reads 0 or garbage
                    out[name] = round(value, 1)
        if services:
            self.cf.release(services)
        return out


class VmStatistics(C.Structure):
    _fields_ = [("free", C.c_uint32), ("active", C.c_uint32), ("inactive", C.c_uint32), ("wired", C.c_uint32),
                ("zero_fill", C.c_uint64), ("reactivations", C.c_uint64), ("pageins", C.c_uint64), ("pageouts", C.c_uint64),
                ("faults", C.c_uint64), ("cow", C.c_uint64), ("lookups", C.c_uint64), ("hits", C.c_uint64),
                ("purges", C.c_uint64), ("purgeable", C.c_uint32), ("speculative", C.c_uint32),
                ("decompressions", C.c_uint64), ("compressions", C.c_uint64), ("swapins", C.c_uint64), ("swapouts", C.c_uint64),
                ("compressor", C.c_uint32), ("throttled", C.c_uint32), ("external", C.c_uint32), ("internal", C.c_uint32),
                ("uncompressed", C.c_uint64)]


class XswUsage(C.Structure):
    _fields_ = [("total", C.c_uint64), ("available", C.c_uint64), ("used", C.c_uint64), ("pagesize", C.c_uint32), ("encrypted", C.c_uint32)]


class Mach:
    """CPU ticks, memory, swap and one process's resource use."""

    def __init__(self):
        libc = C.CDLL(None)
        self.libc = libc
        self.host = _bind(libc, "mach_host_self", C.c_uint32)()
        self.statistics = _bind(libc, "host_statistics", C.c_int, C.c_uint32, C.c_int, V, C.POINTER(C.c_uint32))
        self.statistics64 = _bind(libc, "host_statistics64", C.c_int, C.c_uint32, C.c_int, V, C.POINTER(C.c_uint32))
        self.sysctlbyname = _bind(libc, "sysctlbyname", C.c_int, C.c_char_p, V, C.POINTER(C.c_size_t), V, C.c_size_t)
        self.rusage = _bind(libc, "proc_pid_rusage", C.c_int, C.c_int, C.c_int, V)
        info = (C.c_uint32 * 2)()
        _bind(libc, "mach_timebase_info", C.c_int, V)(info)
        self.ns_per_tick = info[0] / info[1]
        self.page_size = os.sysconf("SC_PAGE_SIZE")

    def sysctl_int(self, name):
        value, size = C.c_uint64(0), C.c_size_t(8)
        if self.sysctlbyname(name.encode(), C.byref(value), C.byref(size), None, 0):
            return None
        return value.value if size.value == 8 else value.value & 0xffffffff

    def sysctl_text(self, name):
        buffer, size = C.create_string_buffer(256), C.c_size_t(256)
        return buffer.value.decode() if self.sysctlbyname(name.encode(), buffer, C.byref(size), None, 0) == 0 else None

    def cpu_ticks(self):
        ticks = (C.c_uint32 * 4)()
        count = C.c_uint32(4)
        if self.statistics(self.host, HOST_CPU_LOAD_INFO, ticks, C.byref(count)):
            return None
        return list(ticks)                 # user, system, idle, nice

    def memory(self):
        vm = VmStatistics()
        count = C.c_uint32(C.sizeof(vm) // 4)
        if self.statistics64(self.host, HOST_VM_INFO64, C.byref(vm), C.byref(count)):
            return {}
        page = self.page_size
        app = (vm.internal - vm.purgeable) * page
        out = {"ram_total": self.sysctl_int("hw.memsize"), "ram_wired": vm.wired * page, "ram_app": app,
               "ram_compressed": vm.compressor * page, "ram_file": vm.external * page, "ram_free": vm.free * page,
               "ram_purgeable": vm.purgeable * page, "pageouts": vm.pageouts * page, "swapouts": vm.swapouts * page,
               "swapins": vm.swapins * page}
        # Activity Monitor's Memory Used
        out["ram_used"] = app + out["ram_wired"] + out["ram_compressed"]
        swap, size = XswUsage(), C.c_size_t(C.sizeof(XswUsage))
        if self.sysctlbyname(b"vm.swapusage", C.byref(swap), C.byref(size), None, 0) == 0:
            out["swap_used"], out["swap_total"] = swap.used, swap.total
        return out

    def process(self, pid):
        """A process's footprint, resident size, CPU seconds and disk bytes, or None."""
        info = (C.c_uint64 * 64)()
        if self.rusage(pid, RUSAGE_INFO_V4, info):
            return None
        # rusage_info_v4: a 16-byte UUID, then 64-bit fields
        cpu = (info[2] + info[3]) * self.ns_per_tick / 1e9
        return {"cpu_seconds": cpu, "pageins": info[6], "resident": info[8], "footprint": info[9],
                "disk_read": info[18], "disk_written": info[19]}


def registry(*arguments):
    """The I/O Registry's answer as plist data, or []."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-a", *arguments], capture_output=True, timeout=5).stdout
        return plistlib.loads(out) if out else []
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        return []


def gpu_frequencies(pstates):
    """The GPU's P-state frequencies in MHz: the device tree's table of at most
    pstates rows whose top is in a GPU's range. Empty when none fits."""
    best = []
    tree = registry("-l", "-p", "IODeviceTree")      # the plane's root: one dict, not a list of matches
    nodes = [tree] if isinstance(tree, dict) else list(tree)
    while nodes:
        node = nodes.pop()
        nodes.extend(node.get("IORegistryEntryChildren") or [])
        for key, value in node.items():
            if not (key.startswith("voltage-states") and key.endswith("-sram") and isinstance(value, bytes)):
                continue
            mhz = [int.from_bytes(value[i:i + 4], "little") / 1e6 for i in range(0, len(value) - 7, 8)]
            mhz = [frequency for frequency in mhz if frequency]      # the table's first row is the OFF state
            if mhz and len(mhz) <= pstates and 500 < max(mhz) < 3000:
                best = mhz
    return best


def cluster_of(channel):
    """A core channel's cluster: EACC_ECPU3 -> EACC."""
    return channel.split("_")[0]


class Hardware:
    def __init__(self):
        self.static = {}
        self.errors = {}
        self.cf = self.mach = self.ioreport = self.temperatures = None
        self.previous_cpu = self.previous_disk = None
        self.power = collections.deque(maxlen=POWER_WINDOW)
        self.previous_disk_at = 0.0
        self.processes = {}
        self.frequencies = None
        self._attempt("corefoundation", lambda: setattr(self, "cf", CoreFoundation()))
        self._attempt("mach", lambda: setattr(self, "mach", Mach()))
        if self.cf:
            self._attempt("ioreport", lambda: setattr(self, "ioreport", IOReport(self.cf)))
            self._attempt("temperatures", lambda: setattr(self, "temperatures", Temperatures(self.cf)))
        if self.mach:
            sysctl = self.mach.sysctl_text
            self.static.update(
                chip=sysctl("machdep.cpu.brand_string"), threads=os.cpu_count(),
                clusters=[{"name": sysctl(f"hw.perflevel{i}.name"),
                           "cores": self.mach.sysctl_int(f"hw.perflevel{i}.logicalcpu")}
                          for i in range(self.mach.sysctl_int("hw.nperflevels") or 0)])
        for node in registry("-rc", "AGXAccelerator", "-d", "1")[:1]:
            self.static.update(gpu_name=node.get("model"), gpu_cores=node.get("gpu-core-count"))

    def _attempt(self, name, call):
        try:
            return call()
        except Exception as error:     # a source this macOS lacks leaves its fields out
            self.errors[name] = f"{type(error).__name__}: {error}"
            return None

    def _ioreport(self, out):
        elapsed, data = self.ioreport.read()
        if elapsed <= 0:
            return
        if data["gpu_joules"] is not None:
            # The energy counter arrives in lumps, so one second's watts swing.
            self.power.append(data["gpu_joules"] / elapsed)
            out["gpu_power"] = sum(self.power) / len(self.power)
        states = data["gpu_states"]
        total = sum(states.values())
        if total:
            active = [(name, ticks) for name, ticks in states.items() if name not in ("OFF", "IDLE", "DOWN")]
            busy = sum(ticks for _, ticks in active)
            out["gpu_active"] = 100.0 * busy / total
            if self.frequencies is None:
                self.frequencies = self._attempt("gpu_frequencies", lambda: gpu_frequencies(len(active))) or []
                if self.frequencies:
                    self.static["gpu_mhz_max"] = max(self.frequencies)
            if self.frequencies and busy:
                top = len(self.frequencies) - 1
                out["gpu_mhz"] = sum(self.frequencies[min(i, top)] * ticks for i, (_, ticks) in enumerate(active)) / busy
        clusters = {}
        for name, core in data["cores"].items():
            total = sum(core.values())
            if total:
                clusters.setdefault(cluster_of(name), []).append(100.0 * (1 - core.get("IDLE", 0) / total))
        out["cpu_clusters"] = {name: sum(load) / len(load) for name, load in clusters.items()}
        idle = data["ane_idle"]
        if idle:
            # Each block's idle ticks fall short of the wall clock while it works.
            full = elapsed * TICKS_PER_SECOND
            out["ane_active"] = max(0.0, min(100.0, 100.0 * sum(1 - min(1.0, t / full) for t in idle.values()) / len(idle)))

    def _temperatures(self, out):
        sensors = self.temperatures.read()
        if sensors:
            out["sensors"] = sensors
            soc = [v for k, v in sensors.items() if "tdie" in k]
            pool = soc or list(sensors.values())
            out["temp_soc"] = sum(pool) / len(pool)
            out["temp_max"] = max(sensors.values())

    def _cpu(self, out):
        ticks = self.mach.cpu_ticks()
        if ticks and self.previous_cpu:
            change = [a - b for a, b in zip(ticks, self.previous_cpu)]
            if sum(change):
                out["cpu"] = 100.0 * (1 - change[2] / sum(change))
        self.previous_cpu = ticks
        out.update(self.mach.memory())
        out["memory_level"] = self.mach.sysctl_int("kern.memorystatus_level")

    def _gpu(self, out):
        for node in registry("-rc", "AGXAccelerator", "-d", "1")[:1]:
            stats = node.get("PerformanceStatistics") or {}
            out["gpu_util"] = stats.get("Device Utilization %")
            out["gpu_tiler"] = stats.get("Tiler Utilization %")
            out["gpu_renderer"] = stats.get("Renderer Utilization %")
            out["gpu_mem_used"] = stats.get("In use system memory")
            out["gpu_mem_alloc"] = stats.get("Alloc system memory")
            out["gpu_recoveries"] = stats.get("recoveryCount")

    def _disk(self, out):
        read = written = 0
        for node in registry("-rc", "IOBlockStorageDriver", "-d", "1"):
            stats = node.get("Statistics") or {}
            read += stats.get("Bytes (Read)") or 0
            written += stats.get("Bytes (Write)") or 0
        now = time.monotonic()
        if self.previous_disk and now > self.previous_disk_at:
            seconds = now - self.previous_disk_at
            out["disk_read_mb"] = max(0, read - self.previous_disk[0]) / seconds / 1048576
            out["disk_write_mb"] = max(0, written - self.previous_disk[1]) / seconds / 1048576
        self.previous_disk, self.previous_disk_at = (read, written), now

    def process(self, pid):
        """One process's use, with its CPU and disk rates since the last call."""
        current = self.mach.process(pid) if self.mach and pid else None
        if current is None:
            return None
        now = time.monotonic()
        last = self.processes.get(pid)
        self.processes[pid] = (now, current)
        if last and now > last[0]:
            seconds = now - last[0]
            current["cpu_percent"] = 100.0 * (current["cpu_seconds"] - last[1]["cpu_seconds"]) / seconds
            current["disk_read_mb"] = max(0, current["disk_read"] - last[1]["disk_read"]) / seconds / 1048576
            current["disk_write_mb"] = max(0, current["disk_written"] - last[1]["disk_written"]) / seconds / 1048576
        return current

    def sample(self):
        out = {}
        if self.ioreport:
            self._attempt("ioreport_read", lambda: self._ioreport(out))
        if self.temperatures:
            self._attempt("temperatures_read", lambda: self._temperatures(out))
        if self.mach:
            self._attempt("cpu_memory", lambda: self._cpu(out))
        self._attempt("gpu", lambda: self._gpu(out))
        self._attempt("disk", lambda: self._disk(out))
        return out


if __name__ == "__main__":
    import json
    hardware = Hardware()
    hardware.sample()
    for _ in range(3):
        time.sleep(1)
        started = time.perf_counter()
        sample = hardware.sample()
        sample.pop("sensors", None)
        print(json.dumps(sample), f"{(time.perf_counter() - started) * 1000:.0f} ms")
    print(hardware.static, hardware.errors)
