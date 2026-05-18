# 🌐 network_monitor.py

A zero-dependency Python script that continuously pings a list of IP addresses
and hostnames, records detailed health metrics to a timestamped CSV, and prints
a configurable summary to the terminal. Designed to run for days or months
unattended to diagnose intermittent network drops.

---

## Features

| Feature | Detail |
|---|---|
| ICMP ping | min / avg / max latency + jitter per cycle |
| DNS resolution timing | Separate from ICMP — distinguishes DNS failures from routing failures |
| HTTP/HTTPS check | Catches hosts that silently block ICMP pings |
| Rolling packet-loss % | Sliding window (default: last 60 s) — smooths blips vs real outages |
| Consecutive failure counter | Per-target streak count |
| Active interface tagging | WiFi / Ethernet / unknown, sampled per cycle |
| Traceroute snapshot | Auto-fires on first failure per target, saved as a `.txt` sidecar |
| macOS notification | Desktop alert after N consecutive failures |
| Three output modes | Verbose, Quiet (hourly), Silent |
| Exit summary | Per-target uptime % printed on Ctrl+C |

---

## Requirements

- **macOS** (uses `ping`, `traceroute`, `route`, `networksetup`, `osascript`)
- **Python 3.10 or later** — no pip installs required, pure stdlib

```bash
python3 --version   # must be 3.10+
```

---

## Quick Start

```bash
# 1. Edit the TARGETS list at the top of the script
# 2. Run it
python3 network_monitor.py
```

The script creates a timestamped CSV in the same folder it is run from:

```
network_health_20260518_143000.csv
```

If a target fails, a traceroute snapshot is also saved alongside it:

```
traceroute_75.75.76.76_20260518_143527.txt
```

---

## Configuration

Edit the `CONFIG` block near the top of `network_monitor.py`:

```python
TARGETS: list[str] = [
    "8.8.8.8",       # Google Public DNS
    "apple.com",     # External hostname — exercises DNS + routing
    "75.75.75.75",   # Comcast DNS (primary)
    "75.75.76.76",   # Comcast DNS (secondary)
    # "192.168.1.1", # Uncomment to add your router/gateway
]

INTERVAL_SECONDS      = 5    # How often to ping each target
PING_COUNT            = 4    # Pings per burst (used for avg / jitter)
PING_TIMEOUT_SECONDS  = 2    # Per-ping timeout
ROLLING_WINDOW        = 12   # Samples for rolling loss  (12 × 5s = 60s)
HTTP_TIMEOUT_SECONDS  = 3    # Set to 0 to disable HTTP checks
ALERT_AFTER_FAILURES  = 3    # Desktop alert threshold (consecutive failures)
OUTPUT_DIR            = Path(".")  # Where to write the CSV
```

---

## Output Modes

Run with `-h` to see all options:

```
python3 network_monitor.py -h
```

### Default — Quiet (hourly summary)

Best for long unattended runs. The terminal is silent except for one block
printed on the hour and whenever a traceroute fires.

```bash
python3 network_monitor.py
```

```
────────────────────────────────────────────────────────────────────────
  HOURLY SUMMARY   2026-05-18 14:00 → 15:00
────────────────────────────────────────────────────────────────────────
  ✅ 8.8.8.8              uptime=100.0%  avg=11.4ms   max=22.1ms   drops=0     [WiFi (en0)]
  ✅ apple.com            uptime=100.0%  avg=19.8ms   max=31.4ms   drops=0     [WiFi (en0)]
  ✅ 75.75.75.75          uptime=100.0%  avg=13.9ms   max=24.6ms   drops=0     [WiFi (en0)]
  ❌ 75.75.76.76          uptime= 70.0%  avg=15.2ms   max=25.2ms   drops=144   worst_streak=6  [WiFi (en0)]
────────────────────────────────────────────────────────────────────────
```

**Pressing Ctrl+C** flushes the current partial hour before printing the
all-time session summary, so you never lose data.

---

### `-v` — Verbose (one line per cycle)

Best for active debugging sessions where you want to watch results in real time.

```bash
python3 network_monitor.py -v
```

```
  ✅ 8.8.8.8              avg=11.8ms   loss=  0.0%  roll=  0.0%  fail=0   http=SKIP
  ✅ apple.com            avg=19.5ms   loss=  0.0%  roll=  0.0%  fail=0   http=200   jitter=1.2ms
  ✅ 75.75.75.75          avg=14.2ms   loss=  0.0%  roll=  0.0%  fail=0   http=SKIP
  ❌ 75.75.76.76          avg=     —   loss=100.0%  roll= 30.0%  fail=3   http=SKIP
  [2026-05-18T14:30:45]  iface=WiFi (en0)  cycle=4.83s
```

---

### `-q` — Silent

Zero terminal output during the run. macOS notifications still fire on
failures. The full session summary is printed on Ctrl+C.

```bash
python3 network_monitor.py -q
```

Useful when running in a background terminal or via `launchd` / `nohup`.

```bash
# Example: run silently in the background, log terminal output to a file
nohup python3 network_monitor.py -q > monitor.log 2>&1 &
```

---

## CSV Column Reference

| Column | Type | Description |
|---|---|---|
| `timestamp` | ISO-8601 | Local time of the cycle |
| `target` | string | As entered in `TARGETS` |
| `resolved_ip` | string | IP after DNS lookup; blank if target is already an IP |
| `dns_resolve_ms` | float | Time to resolve DNS in ms; blank for raw IPs |
| `ping_min_ms` | float | Fastest ICMP reply in the burst |
| `ping_avg_ms` | float | Average ICMP reply — **primary latency column** |
| `ping_max_ms` | float | Slowest ICMP reply in the burst |
| `ping_jitter_ms` | float | Std-dev of the burst — high = unstable link |
| `packet_loss_pct` | float | Loss within this cycle's burst (0–100) |
| `rolling_loss_pct` | float | Loss across the last ~60 s — **best column for spotting drops** |
| `consecutive_failures` | int | Back-to-back cycles with 100% loss |
| `ping_status` | string | `ok` / `partial` / `timeout` / `error` |
| `http_status_code` | string | HTTP HEAD response code, `SKIP`, `ERROR`, or `TIMEOUT` |
| `http_latency_ms` | float | Time to first HTTP byte in ms |
| `interface` | string | `WiFi (en0)`, `Ethernet (en1)`, etc. |

> **Blank cells** in latency columns mean the ping produced no response —
> there is nothing to measure. This is distinct from a 0 ms value.

---

## Loading the CSV into Numbers

### Step 1 — Open the file

In **Finder**, navigate to the folder where you ran the script.
Double-click the `network_health_*.csv` file.

> If it doesn't open in Numbers automatically:
> right-click the file → **Open With** → **Numbers**

Numbers will import the CSV and display it as a table.

---

### Step 2 — Filter to one target

Because all targets are interleaved row-by-row, you should filter to a single
target before charting so the lines are clean.

1. Click the **filter icon** (funnel) in the top-right of the table, or go to
   **Format** (right panel) → **Table** → **Filter**
2. Click **Add a Filter**
3. Choose column: **target** → **is** → type the target name, e.g. `75.75.76.76`
4. Numbers hides all other rows instantly

Repeat with a different filter to compare targets.

---

### Step 3 — Select the columns to chart

Hold **⌘ (Command)** and click each of these column headers to select them:

| Column | Purpose |
|---|---|
| `timestamp` | X-axis |
| `ping_avg_ms` | Primary Y-axis — latency line |
| `rolling_loss_pct` | Secondary Y-axis — packet loss bars |

> **Tip:** You can also add `ping_jitter_ms` as a third series to spot
> instability that precedes a full drop.

---

### Step 4 — Insert the chart

With those columns selected:

1. Click **Insert** in the menu bar → **Chart**
2. Choose **2D Line**
3. Numbers creates a chart in the sheet

---

### Step 5 — Move loss to a secondary axis

`rolling_loss_pct` is 0–100 (%) while `ping_avg_ms` is typically 5–50 (ms).
Putting them on separate axes keeps both readable.

1. Click the chart to select it
2. In the **Format** panel on the right, click **Series**
3. Select the `rolling_loss_pct` series
4. Under **Axis**, change from **Left** to **Right**

---

### Step 6 — Format the X-axis timestamps

By default Numbers may show the raw ISO-8601 string. To clean it up:

1. Click the X-axis labels
2. In the **Format** panel → **Axis** → **Label Format**
3. Choose **Custom**, enter: `HH:mm:ss`

For multi-day runs use `MM-dd HH:mm` instead.

---

### Suggested chart layout

```
  ms (left)                              % (right)
  │                                           │
50│                             ┌─┐           │100
  │                          ┌─┐│ │┌─┐        │
  │                       ┌─┐│ ││ ││ │        │
25│  ~~~~~~~~~~~~~~~~~~~~~ │ ││ ││ ││ │       │ 50
  │ ping_avg_ms (blue)     │ ││ ││ ││ │        │
12│~~~~~~~~~~~~~~~~~~~~~~~~~────────────      │
  │                                           │  0
  └───────────────────────────────────────────┘
       healthy         outage window    recovery
                ▲                  ▲
             first drop        target back up
             (green bar        (green bars shrink
              appears)          as rolling window clears)
```

---

## How to Read the Chart

```
  Latency (ms) — left axis          Packet Loss % — right axis
       │                                    │
  50ms ┤                          ┌─┐       │ 50%
       │                       ┌──┘ └──┐    │
  25ms ┤      ╭─╮               │      │   │ 25%
       │ ─────╯ ╰───────────────╯      ╰── │
  12ms ┤~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ │ 0%
       │                                    │
       └──────────────────────────────────────
            ①        ②       ③        ④
         Healthy   Warning  Outage  Recovery
```

### ① Healthy
- Blue line is **steady and low** (e.g. 10–20 ms)
- Green is **flat at 0%**
- Normal jitter: < 3 ms

### ② Warning
- Blue line **spikes or becomes erratic** — jitter rises above 5 ms
- Green shows a **small blip** (< 25%)
- The host is still reachable but the link is unstable
- This often precedes a full outage

### ③ Outage
- Blue line **disappears** (blank cells = no response to graph)
- Green **spikes and climbs** higher each cycle as `rolling_loss_pct` accumulates
- `consecutive_failures` column increments every 5 s
- A macOS notification fires at the threshold you set

### ④ Recovery
- Blue line **reappears**
- Green **slowly fades** — the rolling window is still "remembering" the
  recent failures and clears over the next ~60 s
- The fade speed tells you how recently the outage ended

---

## Debugging Checklist

Use the CSV columns to answer these questions:

| Question | How to answer |
|---|---|
| **WiFi or Ethernet?** | Filter `interface` column — drops only on `WiFi` = wireless problem |
| **All targets or just one?** | If `8.8.8.8`, `apple.com`, and Comcast DNS all drop at the same timestamp → your router/modem lost the connection |
| **DNS or routing?** | `dns_resolve_ms` spikes but `ping_avg_ms` is normal → DNS issue only |
| **Network or service?** | `ping_status = ok` but `http_status_code = ERROR` on the same row → the service is down, not your network |
| **Where does the path break?** | Open `traceroute_<target>_*.txt` — look for the first hop showing `* * *` |
| **How long was the outage?** | `consecutive_failures × INTERVAL_SECONDS` = duration in seconds |
| **How often does it happen?** | Sort the CSV by `consecutive_failures` descending — the top rows are your worst events |

---

## Running Long-Term

### Keep it running after you close the terminal

```bash
nohup python3 network_monitor.py -q > monitor.log 2>&1 &
echo $! > monitor.pid          # save the process ID
```

To stop it later:

```bash
kill $(cat monitor.pid)
```

### Run automatically at login (launchd)

Create `~/Library/LaunchAgents/com.local.networkmonitor.plist` with:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.local.networkmonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/network_monitor.py</string>
    <string>-q</string>
  </array>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <true/>
  <key>StandardOutPath</key>   <string>/tmp/networkmonitor.log</string>
  <key>StandardErrorPath</key> <string>/tmp/networkmonitor.err</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.local.networkmonitor.plist
```

---

## File Output Reference

| File | Created | Description |
|---|---|---|
| `network_health_YYYYMMDD_HHmmss.csv` | On start | Main data log — one row per target per cycle |
| `traceroute_<target>_<timestamp>.txt` | On first failure | Path snapshot showing where packets stop |
| `monitor.log` | If using `nohup` | Terminal output (hourly summaries + alerts) |
