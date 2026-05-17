"""
seq_common.py - Shared utilities for all SEQ IPv4 merge implementations.

This module is imported by:
  - seq_solution_1_grace_hash.py
  - seq_solution_2_unix_sort.py
  - seq_solution_3_duckdb.py
  - seq_solution_4_polars_isolated.py

Put this in the same directory as the solution scripts.
"""

import os
import sys
import glob
import time
from datetime import datetime, timedelta

# ============================================================================
# DEFAULT CONFIGURATION (each solution can override)
# ============================================================================

# --- Sample mode. None = full file. Set to int for safe testing. ---
DEFAULT_SAMPLE_ROWS = None

# --- File location ---
DEFAULT_INPUT_SYSLOG_DIR = 'SEQ_IPv4'
DEFAULT_INPUT_IPV4_DIR = 'SEQ_IPv4_NEW'
DEFAULT_OUTPUT_DIR = 'OUTPUT'
DEFAULT_SYSLOG_PATTERN = 'SEQ_IPv4_{date}_{hour}.csv'
DEFAULT_IPV4_PATTERN = 'SEQ_IPV4_NEW_{date}_{hour}.csv'

# --- Delimiter ---
DEFAULT_INPUT_SEP = '|'
DEFAULT_OUTPUT_SEP = '|'

# --- Merge configuration ---
MERGE_KEYS = ['START_TIME_MIN', 'MSISDN', 'Private_IP', 'Private_IP_Port']
COLUMNS_TO_ADD = ['Public_NAT_IP', 'Public_NAT_IP_Port']
SYSLOG_NEEDED_COLS = ['START_TIME', 'MSISDN', 'Private_IP',
                      'Private_IP_Port', 'Public_NAT_IP', 'Public_NAT_IP_Port']
START_TIME_COL = 'START_TIME'
START_TIME_MINUTE_COL = 'START_TIME_MIN'


# ============================================================================
# COLORED OUTPUT (auto-disabled when not a TTY)
# ============================================================================

_USE_COLOR = (
    hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
    and os.environ.get('NO_COLOR', '') == ''
)

# Windows 10+ needs a one-time call to enable ANSI processing
if _USE_COLOR and os.name == 'nt':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        _USE_COLOR = False


def _c(code):
    return code if _USE_COLOR else ''


C_RESET = '\033[0m'
C_BOLD = '\033[1m'
C_DIM = '\033[2m'
C_CYAN = '\033[36m'
C_GREEN = '\033[32m'
C_YELLOW = '\033[33m'
C_RED = '\033[31m'
C_BLUE = '\033[34m'


def section(title):
    bar = '\u2500' * 70
    print("\n{0}{1}{2}".format(_c(C_CYAN + C_BOLD), bar, _c(C_RESET)), flush=True)
    print("{0}{1}{2}".format(_c(C_CYAN + C_BOLD), title, _c(C_RESET)), flush=True)
    print("{0}{1}{2}".format(_c(C_CYAN + C_BOLD), bar, _c(C_RESET)), flush=True)


def phase(num, total, title):
    print("\n{0}[{1}/{2}]{3} {4}{5}{6}".format(
        _c(C_BLUE + C_BOLD), num, total, _c(C_RESET),
        _c(C_BOLD), title, _c(C_RESET)), flush=True)


def success(msg):
    print("  {0}\u2713{1} {2}".format(_c(C_GREEN + C_BOLD), _c(C_RESET), msg), flush=True)


def info(label, value, value_color=None):
    color = value_color if value_color is not None else C_BOLD
    print("  {0}{1:24s}{2} {3}{4}{5}".format(
        _c(C_DIM), label, _c(C_RESET),
        _c(color), value, _c(C_RESET)), flush=True)


def warn(msg):
    print("  {0}\u26a0  {1}{2}".format(_c(C_YELLOW + C_BOLD), msg, _c(C_RESET)), flush=True)


def error(msg):
    print("  {0}\u2717 {1}{2}".format(_c(C_RED + C_BOLD), msg, _c(C_RESET)), flush=True)


def detail(msg):
    print("  {0}{1}{2}".format(_c(C_DIM), msg, _c(C_RESET)), flush=True)


# ============================================================================
# MEMORY TRACKING (optional - works without psutil)
# ============================================================================

try:
    import psutil
    _PROC = psutil.Process()
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

_peak_rss_mb = 0.0


def sample_memory():
    """Update peak RSS tracker. Call this periodically during long operations."""
    global _peak_rss_mb
    if not HAS_PSUTIL:
        return 0.0
    try:
        rss_mb = _PROC.memory_info().rss / (1024 * 1024)
        if rss_mb > _peak_rss_mb:
            _peak_rss_mb = rss_mb
        return rss_mb
    except Exception:
        return 0.0


def get_peak_memory_mb():
    return _peak_rss_mb


def reset_peak_memory():
    """Reset the peak tracker. Useful between runs in benchmarks."""
    global _peak_rss_mb
    _peak_rss_mb = 0.0


# ============================================================================
# FILE RESOLUTION (matches the original script's logic)
# ============================================================================

def get_current_datetime():
    """Returns (date, hour) for 1 hour ago - the typical batch target."""
    now = datetime.now() - timedelta(hours=1)
    return now.strftime('%Y%m%d'), now.strftime('%H')


def find_files(directory, pattern, date, hour):
    """Find a file matching pattern. Returns None if not found."""
    full_path = os.path.join(directory, pattern.format(date=date, hour=hour))
    if os.path.exists(full_path):
        return full_path
    matches = glob.glob(os.path.join(directory, pattern.format(date=date, hour='*')))
    return matches[0] if matches else None


def resolve_input_files(syslog_dir, ipv4_dir, syslog_pat, ipv4_pat, date, hour):
    """Resolve syslog + ipv4 file paths, falling back to previous hour if needed."""
    syslog = find_files(syslog_dir, syslog_pat, date, hour)
    ipv4 = find_files(ipv4_dir, ipv4_pat, date, hour)

    if not syslog or not ipv4:
        prev = datetime.now() - timedelta(hours=2)
        prev_date, prev_hour = prev.strftime('%Y%m%d'), prev.strftime('%H')
        syslog = syslog or find_files(syslog_dir, syslog_pat, prev_date, prev_hour)
        ipv4 = ipv4 or find_files(ipv4_dir, ipv4_pat, prev_date, prev_hour)
        if syslog and ipv4:
            return syslog, ipv4, prev_hour

    if not syslog or not ipv4:
        raise FileNotFoundError(
            "Could not locate matching syslog and ipv4 files for {0}_{1}".format(date, hour))
    return syslog, ipv4, hour


# ============================================================================
# TIMESTAMP FORMAT DETECTION
# ============================================================================

def detect_time_format(path, delim):
    """
    Look at first data row to decide if START_TIME is unix epoch int or ISO string.
    Returns 'epoch_seconds' or 'iso_string'.
    """
    import csv as _csv
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
        reader = _csv.reader(f, delimiter=delim)
        header = next(reader)
        if 'START_TIME' not in header:
            raise ValueError("No START_TIME column in {0}; found: {1}".format(
                path, header))
        idx = header.index('START_TIME')
        for row in reader:
            if len(row) > idx and row[idx].strip():
                val = row[idx].strip()
                if val.isdigit() and len(val) >= 9:
                    return 'epoch_seconds'
                else:
                    return 'iso_string'
    raise ValueError("No data rows in {0}".format(path))


def truncate_to_minute_str(val, time_format):
    """
    Convert one START_TIME value to 'YYYY-MM-DD HH:MM' string.
    Used in the per-row inner loops where pandas isn't available/wanted.
    """
    if not val or val == 'nan':
        return ''
    if time_format == 'epoch_seconds':
        try:
            ts = int(val)
            dt = datetime.utcfromtimestamp(ts)
            return dt.strftime('%Y-%m-%d %H:%M')
        except (ValueError, OSError):
            return ''
    else:  # iso_string
        try:
            # Try common formats
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f',
                        '%Y/%m/%d %H:%M:%S'):
                try:
                    dt = datetime.strptime(val[:len(fmt)+2], fmt)
                    return dt.strftime('%Y-%m-%d %H:%M')
                except ValueError:
                    continue
            return ''
        except Exception:
            return ''


# ============================================================================
# COMMON BANNER + FINAL STATS REPORTER
# ============================================================================

def print_banner(solution_name, config):
    """Print the startup banner with all config visible."""
    section(solution_name)
    if config.get('sample_rows'):
        warn("SAMPLE MODE: {0:,} rows per file".format(config['sample_rows']))
        detail("Set SAMPLE_ROWS = None for full run")
    for label, value in config.get('extras', []):
        info(label, value)
    info("Input delimiter:", "{0!r}".format(config.get('input_sep', '|')),
         value_color=C_YELLOW)
    info("Temp directory:", config.get('temp_dir') or '(system default)')


def print_stats(syslog_rows, ipv4_rows, output_rows, matched_rows,
                output_path, elapsed_seconds):
    """Print the final statistics block. Identical across all four solutions."""
    section("STATISTICS")
    total = output_rows
    unmatched = total - matched_rows
    pct = (matched_rows / total * 100) if total else 0

    info("Syslog rows processed:", "{0:,}".format(syslog_rows))
    info("IPv4 rows processed:", "{0:,}".format(ipv4_rows))
    info("Output rows written:", "{0:,}".format(total))

    matched_color = C_GREEN if pct >= 50 else (C_YELLOW if pct >= 5 else C_RED)
    print("  {0}{1:24s}{2} {3}{4:,}{5} {6}({7:.2f}%){8}".format(
        _c(C_DIM), "  matched:", _c(C_RESET),
        _c(matched_color + C_BOLD), matched_rows, _c(C_RESET),
        _c(matched_color), pct, _c(C_RESET)), flush=True)
    print("  {0}{1:24s}{2} {3}{4:,}{5} {6}({7:.2f}%){8}".format(
        _c(C_DIM), "  unmatched:", _c(C_RESET),
        _c(C_DIM + C_BOLD), unmatched, _c(C_RESET),
        _c(C_DIM), 100 - pct, _c(C_RESET)), flush=True)

    print("", flush=True)
    info("Output file:", output_path, value_color=C_GREEN + C_BOLD)
    if HAS_PSUTIL:
        peak = get_peak_memory_mb()
        ram_color = C_GREEN if peak < 4000 else (C_YELLOW if peak < 12000 else C_RED)
        info("Peak RAM (Python):", "{0:,.0f} MB".format(peak),
             value_color=ram_color + C_BOLD)

    print("\n  {0}\u2713{1} {2}DONE{3} in {4}{5:.1f}s{6}".format(
        _c(C_GREEN + C_BOLD), _c(C_RESET),
        _c(C_GREEN + C_BOLD), _c(C_RESET),
        _c(C_BOLD), elapsed_seconds, _c(C_RESET)), flush=True)
    print("{0}{1}{2}".format(_c(C_CYAN + C_BOLD), '\u2500' * 70, _c(C_RESET)),
          flush=True)
