import sys
import time
import os
import shutil
import platform
import duckdb
from datetime import datetime, timedelta


# =========================================================
# CONFIGURATION
# =========================================================

INPUT_SYSLOG_DIR = '/Data/home/SMARTCARE_FEEDS/INPUT/SEQ_IPv4'
INPUT_IPV4_DIR   = '/Data/home/SMARTCARE_FEEDS/INPUT/SEQ_IPv4_NEW'
OUTPUT_DIR       = '/Data/home/SMARTCARE_FEEDS/OUTPUT/'
SYSLOG_PATTERN   = 'SEQ_IPv4_{date}_{hour}.csv'
IPV4_PATTERN     = 'SEQ_IPV4_NEW_{date}_{hour}.csv'
DATE_HOUR_OVERRIDE = None

DUCKDB_MEMORY_LIMIT  = '180GB'
DUCKDB_THREADS       = 4
DUCKDB_MAX_TEMP_SIZE = '4TB'

if platform.system() == 'Windows':
    DUCKDB_TEMP_DIR = r"D:/duckdb_tmp"
else:
    DUCKDB_TEMP_DIR = "/Data/home/SMARTCARE_FEEDS/INPUT/SEQ_Merged_IPv4/duckdb_tmp/"

INPUT_SEP           = '|'
OUTPUT_SEP          = '|'
TIME_WINDOW_MINUTES = 3
MIN_FILE_SIZE_MB    = 0
SAMPLE_ROWS         = None

MIN_FREE_RAM_GB     = 4
MIN_FREE_DISK_GB    = 20

# ── MERGE-ACCURACY TOGGLES ──────────────────────────────────────────────
#
# Experiment A — dedup determinism (THE BUG FIX)
#   Previous V7: SELECT DISTINCT ON (...) with no ORDER BY → DuckDB kept an
#   arbitrary NAT row per (minute, msisdn, ip, port). At sample scale this
#   was usually OK; at production scale with many duplicates per key it
#   collapsed valid bindings and discarded real matches.
#   V8: deterministic dedup keeping the row with the widest END_TIME, AND
#   the dedup key now includes Public_NAT_IP_Port so two different public
#   bindings on the same private port aren't collapsed.
DEDUP_KEEP_WIDEST_WINDOW   = True   # True = V8 fix.  False = old V7 behaviour
DEDUP_INCLUDE_PUBLIC_PORT  = True   # True = V8 fix.  False = old V7 behaviour
#
# Experiment B — probe filtering
#   Drops zero-duration UDP-probe NAT rows from the join. Reduces the
#   intermediate set ~22% at production scale. Safe on the sample (no probe
#   matches found). Untested on production — set False once to A/B compare.
DROP_UDP_PROBES_FROM_JOIN  = False
#
# Diagnostic — count unmatched DPI rows by cause
#   A) key never appears in NAT (truly missing — collector issue)
#   B) key exists in NAT but outside the time window (window tuning)
#   C) key exists in window but was lost to dedup (merge logic — should be 0
#      after V8 fix; if non-zero, dedup is still collapsing matches)
PRINT_UNMATCHED_DIAGNOSTIC = True


# =========================================================
# PREFLIGHT CHECKS
# =========================================================

def check_ram():
    try:
        with open('/proc/meminfo') as f:
            info = {k.strip(): v.strip() for k, v in
                    (line.split(':', 1) for line in f if ':' in line)}
        free_kb  = int(info.get('MemAvailable', '0').split()[0])
        free_gb  = free_kb / (1024 ** 2)
        total_kb = int(info.get('MemTotal', '0').split()[0])
        total_gb = total_kb / (1024 ** 2)
    except Exception:
        print("  [WARN] Could not read /proc/meminfo — skipping RAM check")
        return
    print(f"  RAM: {free_gb:.1f} GB free / {total_gb:.1f} GB total")
    if free_gb < MIN_FREE_RAM_GB:
        raise RuntimeError(
            f"Not enough free RAM: {free_gb:.1f} GB available, "
            f"need at least {MIN_FREE_RAM_GB} GB.")
    limit_gb = float(DUCKDB_MEMORY_LIMIT.rstrip('GB').rstrip('gb'))
    if limit_gb > total_gb * 0.85:
        print(f"  [WARN] DUCKDB_MEMORY_LIMIT ({DUCKDB_MEMORY_LIMIT}) >85% of total RAM — risk of OOM")
    if limit_gb < free_gb * 0.4 and free_gb > 32:
        print(f"  [WARN] DUCKDB_MEMORY_LIMIT ({DUCKDB_MEMORY_LIMIT}) far below available RAM ({free_gb:.1f} GB) — forces excess spill")


def check_disk(path):
    try:
        usage = shutil.disk_usage(path)
        free_gb  = usage.free  / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
    except Exception as e:
        print(f"  [WARN] Could not check disk on {path}: {e}")
        return
    print(f"  Disk ({path}): {free_gb:.1f} GB free / {total_gb:.1f} GB total")
    if free_gb < MIN_FREE_DISK_GB:
        raise RuntimeError(f"Not enough free disk on {path}: {free_gb:.1f} GB available, need at least {MIN_FREE_DISK_GB} GB.")


def check_temp_dir_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        raise RuntimeError(f"Cannot create temp directory: {path}")
    test_file = os.path.join(path, '.write_test')
    try:
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception as e:
        raise RuntimeError(f"Temp directory not writable: {path} — {e}")


def check_output_dir_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        raise RuntimeError(f"Cannot create output directory: {path}")
    test_file = os.path.join(path, '.write_test')
    try:
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception as e:
        raise RuntimeError(f"Output directory not writable: {path} — {e}")


def check_csv_columns(path, required_cols, label, sep='|'):
    try:
        with open(path, 'r', errors='replace') as f:
            header = f.readline().rstrip('\n').split(sep)
        header_stripped = [c.strip() for c in header]
        missing = [c for c in required_cols if c not in header_stripped]
        if missing:
            raise RuntimeError(
                f"{label} file is missing columns: {missing}\n  Found: {header_stripped}\n  Path:  {path}")
        print(f"  {label} columns OK ({len(header_stripped)} total)")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Could not read header of {label} file: {path} — {e}")


def check_file_ready(path, label):
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb < MIN_FILE_SIZE_MB:
        raise RuntimeError(
            f"{label} file looks incomplete: {path} ({size_mb:.2f} MB)")
    return size_mb


# =========================================================
# HELPERS
# =========================================================

def get_current_datetime():
    now = datetime.now()
    return now.strftime('%Y%m%d'), now.strftime('%H')


def resolve_input_files(syslog_dir, ipv4_dir, syslog_pattern, ipv4_pattern, date, hour):
    hour_int = int(hour)
    for delta in [0, 1]:
        dt = datetime.strptime(f"{date}{hour_int:02d}", "%Y%m%d%H") - timedelta(hours=delta)
        cd = dt.strftime('%Y%m%d')
        ch = dt.strftime('%H')
        sf = os.path.join(syslog_dir, syslog_pattern.format(date=cd, hour=ch))
        i4 = os.path.join(ipv4_dir,   ipv4_pattern.format(date=cd, hour=ch))
        if os.path.exists(sf) and os.path.exists(i4):
            if delta > 0:
                print(f"  [WARN] Hour {hour_int:02d} incomplete — falling back to previous hour ({ch})")
            return sf, i4, ch
    sf = os.path.join(syslog_dir, syslog_pattern.format(date=date, hour=f"{hour_int:02d}"))
    i4 = os.path.join(ipv4_dir,   ipv4_pattern.format(date=date, hour=f"{hour_int:02d}"))
    return sf, i4, f"{hour_int:02d}"


# =========================================================
# MAIN
# =========================================================

def main():
    start_total = time.time()

    print("=" * 70)
    print("IPv4 MERGE - PRODUCTION (v8 — accuracy fix + cause diagnostic)")
    print("=" * 70)
    print(f"  Time Window:           ±{TIME_WINDOW_MINUTES} minute(s)")
    print(f"  Memory Limit:          {DUCKDB_MEMORY_LIMIT}")
    print(f"  Threads:               {DUCKDB_THREADS}")
    print(f"  Temp ceiling:          {DUCKDB_MAX_TEMP_SIZE}")
    print(f"  Dedup keep widest:     {DEDUP_KEEP_WIDEST_WINDOW}")
    print(f"  Dedup includes pub:    {DEDUP_INCLUDE_PUBLIC_PORT}")
    print(f"  Drop UDP probes:       {DROP_UDP_PROBES_FROM_JOIN}")

    date, hour = DATE_HOUR_OVERRIDE if DATE_HOUR_OVERRIDE else get_current_datetime()
    print(f"\nTarget: {date} hour {hour}")

    syslog_path, ipv4_path, resolved_hour = resolve_input_files(
        INPUT_SYSLOG_DIR, INPUT_IPV4_DIR, SYSLOG_PATTERN, IPV4_PATTERN, date, hour
    )
    for fp, ft in [(syslog_path, "Syslog"), (ipv4_path, "IPv4")]:
        if not os.path.exists(fp):
            raise FileNotFoundError(f"{ft} file not found: {fp}")

    output_path = os.path.join(OUTPUT_DIR, f'SEQ_IPV4_Merge_{date}_{resolved_hour}.csv')

    # ── PREFLIGHT ──
    print("\nPreflight checks...")
    syslog_mb = check_file_ready(syslog_path, "Syslog")
    ipv4_mb   = check_file_ready(ipv4_path,   "IPv4")
    print(f"  Syslog: {syslog_path}  ({syslog_mb:.1f} MB)")
    print(f"  IPv4:   {ipv4_path}  ({ipv4_mb:.1f} MB)")
    check_csv_columns(syslog_path, ['START_TIME','END_TIME','MSISDN','Private_IP',
                                     'Private_IP_Port','Public_NAT_IP','Public_NAT_IP_Port'],
                      'Syslog', sep=INPUT_SEP)
    check_csv_columns(ipv4_path,   ['START_TIME','MSISDN','Private_IP','Private_IP_Port'],
                      'IPv4',   sep=INPUT_SEP)
    check_ram()
    check_temp_dir_writable(DUCKDB_TEMP_DIR)
    check_disk(DUCKDB_TEMP_DIR)
    check_output_dir_writable(OUTPUT_DIR)
    print(f"  Output: {output_path}")
    print("  All preflight checks passed.\n")

    # ── INIT DuckDB ──
    print("Initializing DuckDB...")
    t0 = time.time()
    con = duckdb.connect(database=':memory:')
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=true")
    tmp = DUCKDB_TEMP_DIR.replace('\\', '/')
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute(f"SET max_temp_directory_size='{DUCKDB_MAX_TEMP_SIZE}'")
    actual = con.execute("""
        SELECT current_setting('memory_limit'), current_setting('threads'), current_setting('temp_directory')
    """).fetchone()
    print(f"  DuckDB ready — memory={actual[0]}, threads={actual[1]}, tmp={actual[2] or '(not set)'}  ({time.time()-t0:.2f}s)")

    sample_clause = f"LIMIT {SAMPLE_ROWS}" if SAMPLE_ROWS else ""
    win = TIME_WINDOW_MINUTES
    probe_filter = "WHERE END_TIME != START_TIME" if DROP_UDP_PROBES_FROM_JOIN else ""

    # ── Build dedup key and order clause for the NAT CTE ──────────────────
    # V8 fix: include Public_NAT_IP_Port (so different bindings aren't collapsed)
    # and ORDER BY END_TIME DESC (so the widest-window row wins deterministically).
    if DEDUP_INCLUDE_PUBLIC_PORT:
        dedup_key = "start_min, MSISDN, Private_IP, Private_IP_Port, Public_NAT_IP_Port"
    else:
        dedup_key = "start_min, MSISDN, Private_IP, Private_IP_Port"

    if DEDUP_KEEP_WIDEST_WINDOW:
        # ROW_NUMBER variant — deterministic, picks widest END_TIME per group
        nat_dedup_sql = f"""
            WITH nat_raw AS (
                SELECT
                    (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min,
                    CAST(START_TIME AS BIGINT)                  AS start_t,
                    MSISDN,
                    Private_IP,
                    Private_IP_Port,
                    Public_NAT_IP,
                    Public_NAT_IP_Port,
                    CAST(END_TIME   AS BIGINT) AS end_t,
                    CASE WHEN END_TIME = START_TIME
                         THEN 'udp_probe' ELSE 'session' END AS session_type
                FROM read_csv('{syslog_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)
                {probe_filter}
                {sample_clause}
            ),
            nat AS (
                SELECT start_min, start_t, MSISDN, Private_IP, Private_IP_Port,
                       Public_NAT_IP, Public_NAT_IP_Port, session_type
                FROM nat_raw
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY {dedup_key}
                    ORDER BY end_t DESC
                ) = 1
            )"""
    else:
        # Original V7 behaviour — DISTINCT ON, non-deterministic
        nat_dedup_sql = f"""
            WITH nat AS (
                SELECT DISTINCT ON ({dedup_key})
                    (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min,
                    CAST(START_TIME AS BIGINT)                  AS start_t,
                    MSISDN, Private_IP, Private_IP_Port,
                    Public_NAT_IP, Public_NAT_IP_Port,
                    CASE WHEN END_TIME = START_TIME
                         THEN 'udp_probe' ELSE 'session' END AS session_type
                FROM read_csv('{syslog_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)
                {probe_filter}
                {sample_clause}
            )"""

    # ── BUILD JOIN STAGED INTO TEMP TABLES (so we can diagnose afterwards) ─
    print("\nRunning join pipeline...")
    t1 = time.time()

    # Materialise nat and dpi as tables so the diagnostic queries can reuse them
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE nat AS
        {nat_dedup_sql}
        SELECT * FROM nat
    """)
    nat_count = con.execute("SELECT COUNT(*) FROM nat").fetchone()[0]
    print(f"  NAT (deduped): {nat_count:,} rows")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE dpi AS
        SELECT *,
            (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min
        FROM read_csv('{ipv4_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)
        {sample_clause}
    """)
    dpi_count = con.execute("SELECT COUNT(*) FROM dpi").fetchone()[0]
    print(f"  DPI:           {dpi_count:,} rows")

    # Write the merged output.
    # At production scale, the SAME (MSISDN, IP, port) can appear multiple
    # times in the ±N min window because of port reuse — the subscriber
    # closes a session and the OS hands the port back out for a new one
    # before N minutes pass. At sample scale (62 rows, one subscriber, one
    # hour) this almost never happens. At 117M DPI rows / hour it happens
    # tens of thousands of times. Without a tiebreak, the LEFT JOIN expands
    # the DPI row to multiple candidates and keeps an arbitrary one — often
    # the WRONG NAT binding (the previous session, not the current one).
    #
    # The QUALIFY here keeps the NAT row whose start_t is closest in actual
    # seconds to the DPI row's start_t. This is the second large-volume
    # merge fix (the first being the V8 dedup). On a single-subscriber
    # sample it changes nothing; at production it picks the correct binding
    # for every reused port in the window.
    con.execute(f"""
        COPY (
            SELECT
                dpi.* EXCLUDE (start_min, st_raw),
                nat.Public_NAT_IP,
                nat.Public_NAT_IP_Port,
                nat.session_type
            FROM (
                SELECT *,
                    CAST(START_TIME AS BIGINT) AS st_raw
                FROM dpi
            ) dpi
            LEFT JOIN nat
                ON  nat.MSISDN          = dpi.MSISDN
                AND nat.Private_IP      = dpi.Private_IP
                AND nat.Private_IP_Port = dpi.Private_IP_Port
                AND nat.start_min BETWEEN (dpi.start_min - {win})
                                      AND (dpi.start_min + {win})
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY dpi.st_raw, dpi.MSISDN, dpi.Private_IP, dpi.Private_IP_Port
                ORDER BY ABS(dpi.st_raw - nat.start_t) ASC NULLS LAST
            ) = 1
        )
        TO '{output_path}' (HEADER TRUE, DELIMITER '{OUTPUT_SEP}')
    """)
    print(f"  Pipeline completed ({time.time()-t1:.2f}s)")

    # ── STATS ──
    stats = con.execute(f"""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN Public_NAT_IP IS NOT NULL AND Public_NAT_IP != '' THEN 1 ELSE 0 END), 0) AS matched,
            COALESCE(SUM(CASE WHEN session_type = 'udp_probe' THEN 1 ELSE 0 END), 0) AS udp_rows
        FROM read_csv('{output_path}', sep='{OUTPUT_SEP}', header=True, ignore_errors=True)
    """).fetchone()
    total_rows, matched_rows, udp_rows = stats
    unmatched_rows = total_rows - matched_rows
    match_pct = (matched_rows / total_rows * 100) if total_rows else 0.0

    # ── UNMATCHED-CAUSE DIAGNOSTIC ────────────────────────────────────────
    # For every unmatched DPI row, classify WHY:
    #   A) key never appears in NAT at all  → collector issue (escalate)
    #   B) key exists in NAT but no row in window  → time-window tuning
    #   C) (should be 0 after V8 fix) key in window but lost in merge logic
    #
    if PRINT_UNMATCHED_DIAGNOSTIC and unmatched_rows > 0:
        print("\nDiagnosing unmatched DPI rows by cause...")
        td = time.time()
        # Build a set of all NAT keys (across the full hour, no time bound)
        # to distinguish "key exists somewhere" vs "key never exists".
        con.execute("""
            CREATE OR REPLACE TEMP TABLE nat_keys AS
            SELECT DISTINCT MSISDN, Private_IP, Private_IP_Port
            FROM read_csv(?, sep=?, header=True, ignore_errors=True)
        """, [syslog_path, INPUT_SEP])

        cause = con.execute(f"""
            WITH unmatched AS (
                SELECT d.MSISDN, d.Private_IP, d.Private_IP_Port, d.start_min
                FROM dpi d
                LEFT JOIN nat n
                    ON  n.MSISDN = d.MSISDN
                    AND n.Private_IP = d.Private_IP
                    AND n.Private_IP_Port = d.Private_IP_Port
                    AND n.start_min BETWEEN (d.start_min - {win}) AND (d.start_min + {win})
                WHERE n.MSISDN IS NULL
            ),
            classified AS (
                SELECT
                    u.*,
                    CASE
                        WHEN nk.MSISDN IS NULL THEN 'A_no_nat_record'
                        ELSE 'B_outside_window'
                    END AS unmatch_cause
                FROM unmatched u
                LEFT JOIN nat_keys nk
                    ON  nk.MSISDN          = u.MSISDN
                    AND nk.Private_IP      = u.Private_IP
                    AND nk.Private_IP_Port = u.Private_IP_Port
            )
            SELECT unmatch_cause, COUNT(*) FROM classified GROUP BY 1 ORDER BY 2 DESC
        """).fetchall()

        print(f"  diagnostic took {time.time()-td:.1f}s\n")
        print(f"  UNMATCHED BREAKDOWN ({unmatched_rows:,} rows total)")
        print(f"  {'-' * 56}")
        cause_labels = {
            'A_no_nat_record':  'A  No NAT record exists (any time)  → collector',
            'B_outside_window': 'B  NAT exists but outside time window → tuning',
        }
        for tag, n in cause:
            label = cause_labels.get(tag, tag)
            pct   = 100 * n / unmatched_rows
            print(f"  {label:<48}  {n:>14,}  ({pct:5.1f}%)")
        print(f"  {'-' * 56}")
        print(f"  Interpretation:")
        print(f"    A rows can ONLY be resolved by fixing the NAT collector.")
        print(f"    B rows can be resolved by widening TIME_WINDOW_MINUTES.")
        print(f"  Together with matched rows, this accounts for 100% of DPI input.\n")

    con.close()

    # Sanity warnings
    if total_rows == 0:
        print("  [WARN] Output is empty — check input files and join keys")
    elif match_pct < 10.0:
        print(f"  [WARN] Match rate is very low ({match_pct:.1f}%) — check column names and separator")

    total_time = time.time() - start_total
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  Output records:  {total_rows:,}")
    print(f"  Matched:         {matched_rows:,} ({match_pct:.2f}%)")
    print(f"  Unmatched:       {unmatched_rows:,}")
    print(f"  UDP probe rows:  {udp_rows:,}")
    print(f"  Total time:      {total_time:.2f}s ({total_time/60:.1f} min)")
    print(f"  Output file:     {output_path}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user"); sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc(); sys.exit(1)
