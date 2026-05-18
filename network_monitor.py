#!/usr/bin/env python3
"""
network_monitor.py
──────────────────
Monitors network health by pinging (ICMP) and HTTP-checking a list of
targets at a regular interval. All results are appended to a timestamped
CSV that you can drag straight into Numbers, Excel, or Grapher.

Features
--------
  • ICMP ping  – min / avg / max latency + jitter
  • DNS resolution timing  – separate from ICMP so you can tell them apart
  • HTTP/HTTPS head-check  – catches hosts that block ICMP pings
  • Rolling packet-loss %  – sliding window (default: last 60 s)
  • Consecutive failure counter  – per target
  • Active interface tagging  – WiFi / Ethernet / unknown, sampled per cycle
  • Traceroute snapshot  – fires once on the *first* failure per target,
                           saved next to the CSV as traceroute_<target>.txt
  • macOS notification  – desktop alert after N consecutive failures
  • Summary report  – printed on Ctrl+C / SIGTERM

Zero third-party dependencies  (stdlib + macOS system tools only)

Usage
-----
  1. Edit the CONFIG section below.
  2. python3 network_monitor.py
  3. Press Ctrl+C to stop — summary is printed and CSV path is shown.
"""

import argparse
import csv
import datetime
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CONFIG  — edit this section
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TARGETS: list[str] = [
    "8.8.8.8",         # Google Public DNS (good baseline — never goes down)
    "1.1.1.1",         # Cloudflare DNS
    "google.com",      # External hostname — exercises DNS + routing
    "apple.com",       # Another external hostname
    # "192.168.1.1",   # Uncomment to add your router/gateway
]

# How often to run a full check cycle (seconds).
# With MONITOR_ALL_INTERFACES=True the effective work per cycle doubles
# (both WiFi and Ethernet are tested), so keep this ≥ 5 s.
INTERVAL_SECONDS: int = 5

# Number of ICMP pings per target per cycle (avg / jitter calculated across these).
# Higher values give more accurate jitter readings but slow each cycle down.
PING_COUNT: int = 4

# Per-ping timeout in seconds. Any ping that exceeds this counts as lost (100% loss
# for that packet). macOS converts this to milliseconds internally — handled below.
PING_TIMEOUT_SECONDS: int = 2

# Rolling window size for rolling_loss_pct — measured in SAMPLES, not seconds.
# Effective time window = ROLLING_WINDOW × INTERVAL_SECONDS  (default: 12 × 5 s = 60 s)
# If you change INTERVAL_SECONDS, adjust this to keep the same time window.
ROLLING_WINDOW: int = 12

# Timeout for the HTTP HEAD check in seconds. Set to 0 to disable HTTP checks entirely.
# The check issues a HEAD request to the target — useful for hosts that block ICMP pings.
HTTP_TIMEOUT_SECONDS: int = 3

# Number of consecutive 100%-loss cycles before a macOS desktop notification fires.
# Tracked independently per target AND per interface — a WiFi drop on 8.8.8.8 will
# alert separately from an Ethernet drop on the same host.
ALERT_AFTER_FAILURES: int = 3

# Directory where the CSV and traceroute snapshot files are written.
# "." means the folder you run the script from.
OUTPUT_DIR: Path = Path(".")

# True  — ping every target via WiFi AND Ethernet simultaneously.
#         Each cycle produces two CSV rows per target (one per interface) so you
#         can directly compare whether a drop is wireless-only or affects both paths.
#         Falls back gracefully if only one interface is active.
# False — ping via the first active interface found only (typically Ethernet on a
#         Mac mini, WiFi on a MacBook). Halves the number of CSV rows per cycle.
MONITOR_ALL_INTERFACES: bool = True

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CSV schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CSV_FIELDS = [
    "timestamp",            # ISO-8601 local time
    "target",               # As entered in TARGETS
    "resolved_ip",          # IP address after DNS lookup
    "dns_resolve_ms",       # Time to resolve DNS (ms); blank if target is already an IP
    "ping_min_ms",          # Fastest ICMP reply in the burst
    "ping_avg_ms",          # Average ICMP reply
    "ping_max_ms",          # Slowest ICMP reply
    "ping_jitter_ms",       # Std-dev across the burst (macOS reports this directly)
    "packet_loss_pct",      # Loss within this cycle's ping burst (0–100)
    "rolling_loss_pct",     # Loss across the last ROLLING_WINDOW cycles
    "consecutive_failures", # How many back-to-back cycles had 100% loss
    "ping_status",          # ok | partial | timeout | error
    "http_status_code",     # HTTP HEAD response code, or "SKIP" / "ERROR" / "TIMEOUT"
    "http_latency_ms",      # Time to first byte for HTTP check (ms)
    "interface",            # WiFi | Ethernet | unknown
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI arguments
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Network health monitor — pings targets and logs to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
output modes:
  (default)   Quiet  — one summary block per hour, printed on the hour
  -v          Verbose — one line per target per cycle  (~every 5 s)
  -q          Silent  — no periodic output; alerts + final summary only
        """,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print one line per target per cycle (good for active debugging)",
    )
    group.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="No periodic output — only failure alerts and the exit summary",
    )
    return parser.parse_args()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Per-target state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TargetState:
    def __init__(self, target: str) -> None:
        self.target = target
        self.consecutive_failures: int = 0
        self.rolling: deque[bool] = deque(maxlen=ROLLING_WINDOW)  # True = success
        self.traceroute_done: bool = False          # Fire traceroute only once
        self.total_cycles: int = 0
        self.total_ok: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Hourly summary accumulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HourlySummary:
    """Accumulates per-target stats across one clock hour."""

    def __init__(self) -> None:
        self.cycles: int = 0
        self.ok_cycles: int = 0
        self.latencies: list[float] = []
        self.max_latency: float = 0.0
        self.drop_cycles: int = 0
        self.max_consec: int = 0
        self.interfaces: set[str] = set()

    def update(
        self,
        success: bool,
        avg_ms: float | None,
        max_ms: float | None,
        consec: int,
        iface: str,
    ) -> None:
        self.cycles += 1
        if success:
            self.ok_cycles += 1
            if avg_ms is not None:
                self.latencies.append(avg_ms)
            if max_ms is not None:
                self.max_latency = max(self.max_latency, max_ms)
        else:
            self.drop_cycles += 1
        self.max_consec = max(self.max_consec, consec)
        self.interfaces.add(iface)

    def render(self, target: str) -> str:
        uptime = (self.ok_cycles / self.cycles * 100) if self.cycles else 0.0
        avg    = sum(self.latencies) / len(self.latencies) if self.latencies else None
        icon   = "✅" if uptime >= 99 else ("⚠️ " if uptime >= 80 else "❌")
        avg_s  = f"{avg:.1f}ms"              if avg is not None    else "      —"
        max_s  = f"{self.max_latency:.1f}ms" if self.max_latency   else "     —"
        streak = f"  worst_streak={self.max_consec}" if self.max_consec else ""
        iface  = "/".join(sorted(self.interfaces))
        return (
            f"  {icon} {target:<22}  uptime={uptime:>5.1f}%  "
            f"avg={avg_s:<9}  max={max_s:<9}  "
            f"drops={self.drop_cycles:<4}{streak}  [{iface}]"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DNS resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def resolve_dns(hostname: str) -> tuple[str | None, float | None]:
    """Return (resolved_ip, elapsed_ms) or (None, None) on failure."""
    # Skip resolution if the target is already a bare IP address
    try:
        socket.inet_aton(hostname)   # raises OSError if not an IPv4 literal
        return hostname, None
    except OSError:
        pass
    try:
        t0 = time.monotonic()
        ip = socket.gethostbyname(hostname)
        elapsed_ms = (time.monotonic() - t0) * 1000
        return ip, round(elapsed_ms, 2)
    except socket.gaierror:
        return None, None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ICMP ping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ping_host(host: str) -> dict:
def ping_host(host: str, source_ip: str = "") -> dict:
    """
    Run the system `ping` command and parse the results.
    source_ip: if set, forces the ping out that specific interface via -S.
    Returns a dict with keys: min, avg, max, jitter, loss, status.
    """
    is_macos = platform.system() == "Darwin"

    if is_macos:
        cmd = [
            "ping", "-c", str(PING_COUNT),
            "-W", str(PING_TIMEOUT_SECONDS * 1000),
        ]
        if source_ip:
            cmd += ["-S", source_ip]
        cmd.append(host)
    else:
        cmd = [
            "ping", "-c", str(PING_COUNT),
            "-W", str(PING_TIMEOUT_SECONDS),
        ]
        if source_ip:
            cmd += ["-I", source_ip]   # Linux uses -I for source interface/IP
        cmd.append(host)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT_SECONDS * PING_COUNT + 5,
        )
        return _parse_ping(proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        return _empty_ping("timeout")
    except Exception as exc:
        return _empty_ping(f"error:{exc}")


def _parse_ping(output: str) -> dict:
    # Packet loss percentage
    loss_m = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    loss = float(loss_m.group(1)) if loss_m else 100.0

    # RTT summary line — macOS & Linux both emit min/avg/max/stddev
    rtt_m = re.search(
        r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)",
        output,
    )
    if rtt_m:
        rtt_min, rtt_avg, rtt_max, jitter = (float(x) for x in rtt_m.groups())
        status = "ok" if loss == 0 else ("partial" if loss < 100 else "timeout")
        return dict(min=rtt_min, avg=rtt_avg, max=rtt_max, jitter=jitter,
                    loss=loss, status=status)
    else:
        status = "timeout" if loss >= 100 else "partial"
        return _empty_ping(status, loss=loss)


def _empty_ping(status: str, loss: float = 100.0) -> dict:
    return dict(min=None, avg=None, max=None, jitter=None, loss=loss, status=status)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTTP check
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def http_check(target: str) -> tuple[str, float | None]:
    """
    Issue an HTTP HEAD request to the target.
    Returns (status_code_or_label, latency_ms_or_None).
    """
    if HTTP_TIMEOUT_SECONDS == 0:
        return "SKIP", None

    # Build a URL — add https:// if the target looks like a hostname
    if target.startswith(("http://", "https://")):
        url = target
    elif re.match(r"^\d+\.\d+\.\d+\.\d+$", target):
        # Raw IP — HTTP may not be meaningful; skip
        return "SKIP", None
    else:
        url = f"https://{target}"

    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", "network-monitor/1.0")
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            latency_ms = round((time.monotonic() - t0) * 1000, 2)
            return str(resp.status), latency_ms
    except urllib.error.HTTPError as exc:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)  # type: ignore[possibly-undefined]
        return str(exc.code), latency_ms
    except urllib.error.URLError:
        return "ERROR", None
    except TimeoutError:
        return "TIMEOUT", None
    except Exception:
        return "ERROR", None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Active interface detection  (macOS)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Each entry is (device, source_ip, label) e.g. ("en0", "192.168.1.5", "WiFi (en0)")
InterfaceInfo = tuple[str, str, str]

def get_active_interfaces() -> list[InterfaceInfo]:
    """
    Return one entry per active network interface (WiFi + Ethernet).
    Each entry is (device_name, source_ip, human_label).
    Falls back to a single no-source entry on non-macOS or any error.
    """
    if platform.system() != "Darwin":
        return [("", "", "unknown")]

    results: list[InterfaceInfo] = []
    try:
        hw = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5,
        )
        # Each block is separated by a blank line
        for block in hw.stdout.strip().split("\n\n"):
            port, device = "", ""
            for line in block.strip().splitlines():
                if line.startswith("Hardware Port:"):
                    port = line.split(":", 1)[1].strip()
                elif line.startswith("Device:"):
                    device = line.split(":", 1)[1].strip()

            if not device:
                continue
            port_lower = port.lower()
            if "wi-fi" in port_lower or "airport" in port_lower:
                kind = "WiFi"
            elif "ethernet" in port_lower or "thunderbolt" in port_lower:
                kind = "Ethernet"
            else:
                continue  # skip Bluetooth, FireWire, etc.

            # Only include if the interface actually has an IP right now
            ip_proc = subprocess.run(
                ["ipconfig", "getifaddr", device],
                capture_output=True, text=True, timeout=3,
            )
            source_ip = ip_proc.stdout.strip()
            if source_ip:
                results.append((device, source_ip, f"{kind} ({device})"))

    except Exception:
        pass

    # Fallback: single entry with no forced source (uses OS default route)
    return results if results else [("", "", "unknown")]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Traceroute  (fires once per target on first failure)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_traceroute(target: str, output_dir: Path) -> None:
    safe_name = re.sub(r"[^\w\-.]", "_", target)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"traceroute_{safe_name}_{ts}.txt"

    cmd = (
        ["traceroute", "-m", "20", "-w", "2", target]
        if platform.system() == "Darwin"
        else ["traceroute", "-m", "20", "-w", "2", target]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out_path.write_text(
            f"# Traceroute to {target}  —  {ts}\n\n"
            + result.stdout
            + (("\n\n# stderr:\n" + result.stderr) if result.stderr.strip() else "")
        )
        print(f"  🗺  Traceroute saved → {out_path.name}")
    except Exception as exc:
        print(f"  ⚠️  Traceroute failed: {exc}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  macOS notification
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def notify(title: str, message: str) -> None:
    if platform.system() == "Darwin":
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CSV helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def init_csv(filepath: Path) -> None:
    with open(filepath, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def append_csv(filepath: Path, row: dict) -> None:
    with open(filepath, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Summary report  (printed on exit)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_summary(
    states: dict[tuple[str, str], TargetState],
    ifaces: list[InterfaceInfo],
    csv_path: Path,
) -> None:
    print("\n" + "━" * 62)
    print("  SUMMARY")
    print("━" * 62)
    for _dev, _ip, iface_label in ifaces:
        print(f"\n  {iface_label}")
        for target in TARGETS:
            st = states.get((target, iface_label))
            if st is None or st.total_cycles == 0:
                continue
            uptime = (st.total_ok / st.total_cycles) * 100
            icon = "✅" if uptime >= 99 else ("⚠️ " if uptime >= 80 else "❌")
            print(f"    {icon}  {target:<22}  uptime={uptime:.1f}%  "
                  f"({st.total_ok}/{st.total_cycles} cycles ok)")
    print("\n" + "━" * 62)
    print(f"  📄  CSV saved to:  {csv_path.resolve()}")
    print("━" * 62 + "\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main loop
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run() -> None:
    args = parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_start = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"network_health_{ts_start}.csv"
    init_csv(csv_path)

    # ── Discover active interfaces ────────────────────────────────────────────
    all_ifaces = get_active_interfaces()
    ifaces = all_ifaces if MONITOR_ALL_INTERFACES else all_ifaces[:1]

    # State is keyed by (target, iface_label) so WiFi and Ethernet are tracked
    # independently — a WiFi drop won't pollute the Ethernet consecutive counter.
    StateKey = tuple[str, str]
    states:     dict[StateKey, TargetState]   = {}
    hour_stats: dict[StateKey, HourlySummary] = {}
    for target in TARGETS:
        for _dev, _ip, iface_label in ifaces:
            key = (target, iface_label)
            states[key]     = TargetState(target)
            hour_stats[key] = HourlySummary()

    mode_label = (
        "verbose  (one line per cycle)"        if args.verbose else
        "silent   (alerts + exit summary only)" if args.quiet   else
        "quiet    (hourly summary)"
    )
    iface_names = "  +  ".join(lbl for _, _, lbl in ifaces)
    print(f"\n{'━'*62}")
    print(f"  🌐  Network Monitor  —  {len(TARGETS)} target(s)")
    print(f"  🔌  Interfaces:  {iface_names}")
    print(f"  ⏱   Interval: {INTERVAL_SECONDS}s   Pings/cycle: {PING_COUNT}")
    print(f"  🖥   Output mode: {mode_label}")
    print(f"  📄  CSV: {csv_path.resolve()}")
    print(f"  Press Ctrl+C to stop")
    print(f"{'━'*62}\n")

    # ── Hourly summary state ──────────────────────────────────────────────────

    def _hour_key() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:00")

    active_hour = _hour_key()

    def flush_hour(label: str) -> None:
        end_dt    = datetime.datetime.strptime(label, "%Y-%m-%d %H:00") + datetime.timedelta(hours=1)
        end_label = end_dt.strftime("%H:%M")
        print(f"\n{'─'*72}")
        print(f"  HOURLY SUMMARY   {label} → {end_label}")
        print(f"{'─'*72}")
        for _dev, _ip, iface_label in ifaces:
            print(f"\n  {iface_label}")
            for t in TARGETS:
                print(hour_stats[(t, iface_label)].render(t))
        print(f"{'─'*72}\n")

    # ── Signal handlers ───────────────────────────────────────────────────────

    def shutdown(sig, frame):  # noqa: ANN001
        if not args.verbose and not args.quiet:
            flush_hour(active_hour)
        print_summary(states, ifaces, csv_path)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Main loop ─────────────────────────────────────────────────────────────

    while True:
        cycle_start = time.monotonic()
        ts = datetime.datetime.now().isoformat(timespec="seconds")

        for iface_dev, source_ip, iface_label in ifaces:
            # DNS is resolved once per target per cycle (same result for both ifaces)
            dns_cache: dict[str, tuple[str | None, float | None]] = {}

            for target in TARGETS:
                key = (target, iface_label)
                st  = states[key]
                st.total_cycles += 1

                # ── DNS ───────────────────────────────────────────────────────
                if target not in dns_cache:
                    dns_cache[target] = resolve_dns(target)
                resolved_ip, dns_ms = dns_cache[target]
                ping_host_addr = resolved_ip or target

                # ── ICMP ping (forced via this interface) ─────────────────────
                ping = ping_host(ping_host_addr, source_ip=source_ip)

                # ── HTTP check (only on first interface to avoid duplicate hits)
                if iface_dev == ifaces[0][0]:
                    http_code, http_ms = http_check(target)
                else:
                    http_code, http_ms = "SKIP", None

                # ── State update ──────────────────────────────────────────────
                success = ping["loss"] < 100.0
                st.rolling.append(success)
                rolling_loss = round(
                    (1 - sum(st.rolling) / len(st.rolling)) * 100, 1
                )

                if success:
                    st.total_ok += 1
                    st.consecutive_failures = 0
                else:
                    st.consecutive_failures += 1
                    if st.consecutive_failures == ALERT_AFTER_FAILURES:
                        notify(
                            "⚠️ Network Drop",
                            f"{target} via {iface_label} failed {ALERT_AFTER_FAILURES}× in a row",
                        )
                    if not st.traceroute_done:
                        st.traceroute_done = True
                        run_traceroute(target, OUTPUT_DIR)

                # ── Hourly accumulator ────────────────────────────────────────
                hour_stats[key].update(
                    success, ping["avg"], ping["max"], st.consecutive_failures, iface_label
                )

                # ── CSV row ───────────────────────────────────────────────────
                row: dict = {
                    "timestamp":            ts,
                    "target":               target,
                    "resolved_ip":          resolved_ip or "UNRESOLVED",
                    "dns_resolve_ms":       dns_ms if dns_ms is not None else "",
                    "ping_min_ms":          ping["min"] if ping["min"] is not None else "",
                    "ping_avg_ms":          ping["avg"] if ping["avg"] is not None else "",
                    "ping_max_ms":          ping["max"] if ping["max"] is not None else "",
                    "ping_jitter_ms":       ping["jitter"] if ping["jitter"] is not None else "",
                    "packet_loss_pct":      ping["loss"],
                    "rolling_loss_pct":     rolling_loss,
                    "consecutive_failures": st.consecutive_failures,
                    "ping_status":          ping["status"],
                    "http_status_code":     http_code,
                    "http_latency_ms":      http_ms if http_ms is not None else "",
                    "interface":            iface_label,
                }
                append_csv(csv_path, row)

                # ── Verbose console output ────────────────────────────────────
                if args.verbose:
                    icon = "✅" if success else "❌"
                    lat  = f"{ping['avg']}ms" if ping["avg"] else "     —"
                    jit  = f"jitter={ping['jitter']}ms" if ping["jitter"] else ""
                    print(
                        f"  {icon} {iface_label:<18} {target:<22} avg={lat:<9} "
                        f"loss={ping['loss']:>5.1f}%  roll={rolling_loss:>5.1f}%  "
                        f"fail={st.consecutive_failures:<3}  {jit}"
                    )

            if args.verbose:
                print()  # blank line between interfaces in verbose mode

        # ── Verbose: end-of-cycle footer ──────────────────────────────────────
        elapsed = time.monotonic() - cycle_start
        if args.verbose:
            print(f"  [{ts}]  cycle={elapsed:.2f}s\n")

        # ── Hourly rollover ───────────────────────────────────────────────────
        this_hour = _hour_key()
        if this_hour != active_hour:
            if not args.quiet:
                flush_hour(active_hour)
            active_hour = this_hour
            for key in hour_stats:
                hour_stats[key] = HourlySummary()

        time.sleep(max(0.0, INTERVAL_SECONDS - elapsed))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    run()
