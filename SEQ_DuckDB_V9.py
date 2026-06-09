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
TIME_WINDOW_MINUTES = 2
MIN_FILE_SIZE_MB    = 0
SAMPLE_ROWS         = None

MIN_FREE_RAM_GB     = 4
MIN_FREE_DISK_GB    = 20

# ── PROCESSING TOGGLES ──────────────────────────────────────────────
#
# PERFECT DUPLICATE REMOVAL ONLY:
#   - Removes rows that are 100% identical (all columns match)
#   - Keeps rows that differ in ANY column (including timestamps)
#   - Does NOT do key-based deduplication (keeps multiple NAT bindings)
#
DROP_UDP_PROBES_FROM_JOIN  = False
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
    print("IPv4 MERGE - PERFECT DUPLICATE REMOVAL ONLY")
    print("=" * 70)
    print(f"  Time Window:           ±{TIME_WINDOW_MINUTES} minute(s)")
    print(f"  Memory Limit:          {DUCKDB_MEMORY_LIMIT}")
    print(f"  Threads:               {DUCKDB_THREADS}")
    print(f"  Temp ceiling:          {DUCKDB_MAX_TEMP_SIZE}")
    print(f"  Drop UDP probes:       {DROP_UDP_PROBES_FROM_JOIN}")
    print(f"  ✅ Duplicate handling:  PERFECT IDENTICAL ROWS ONLY")
    print(f"  ✅ Join safety:         QUALIFY keeps best match (1 per DPI row)")

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

    # ── NAT CTE - PERFECT DUPLICATE REMOVAL ONLY (keep all unique rows) ──
    # Uses DISTINCT to remove only 100% identical rows
    # No key-based deduplication - keeps multiple NAT bindings for same keys
    nat_sql = f"""
        CREATE OR REPLACE TEMP TABLE nat AS
        SELECT DISTINCT
            (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min,
            CAST(START_TIME AS BIGINT)                  AS start_t,
            MSISDN,
            Private_IP,
            Private_IP_Port,
            Public_NAT_IP,
            Public_NAT_IP_Port,
            CAST(END_TIME AS BIGINT)                    AS end_t,
            CASE WHEN END_TIME = START_TIME
                 THEN 'udp_probe' ELSE 'session' END    AS session_type
        FROM read_csv('{syslog_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)
        {probe_filter}
        {sample_clause}
    """

    # ── DPI CTE - PERFECT DUPLICATE REMOVAL ONLY (keep all unique rows) ──
    # Uses DISTINCT to remove only 100% identical rows
    dpi_sql = f"""
        CREATE OR REPLACE TEMP TABLE dpi_raw AS
        SELECT DISTINCT
            (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min,
            CAST(START_TIME AS BIGINT)                  AS st_raw,
            MSISDN,
            Private_IP,
            Private_IP_Port
        FROM read_csv('{ipv4_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)
        {sample_clause}
    """

    # ── BUILD JOIN WITH SAFEGUARD (QUALIFY prevents explosion) ────────────
    print("\nRunning join pipeline (perfect duplicates removed, best match kept)...")
    t1 = time.time()

    # Materialise tables
    con.execute(nat_sql)
    nat_count = con.execute("SELECT COUNT(*) FROM nat").fetchone()[0]
    print(f"  NAT (unique rows only, no key-based dedup): {nat_count:,} rows")
    
    # Get original DPI count for comparison
    con.execute("""
        CREATE OR REPLACE TEMP TABLE dpi_raw_count AS
        SELECT * FROM read_csv('$ipv4_path', sep='$sep', header=True, ignore_errors=True)
    """.replace('$ipv4_path', ipv4_path).replace('$sep', INPUT_SEP))
    dpi_original_count = con.execute("SELECT COUNT(*) FROM dpi_raw_count").fetchone()[0]
    
    con.execute(dpi_sql)
    dpi_count = con.execute("SELECT COUNT(*) FROM dpi_raw").fetchone()[0]
    print(f"  DPI original:        {dpi_original_count:,} rows")
    print(f"  DPI (unique rows only, no key-based dedup): {dpi_count:,} rows")
    if dpi_original_count > dpi_count:
        print(f"  ✅ Removed {dpi_original_count - dpi_count:,} perfect duplicate rows ({100*(dpi_original_count-dpi_count)/dpi_original_count:.2f}%)")

    # Write the merged output with QUALIFY to prevent explosion
    # QUALIFY ensures exactly 1 output row per DPI row (closest NAT match in time)
    con.execute(f"""
        COPY (
            SELECT
                dpi.start_min,
                dpi.st_raw,
                dpi.MSISDN,
                dpi.Private_IP,
                dpi.Private_IP_Port,
                nat.Public_NAT_IP,
                nat.Public_NAT_IP_Port,
                nat.session_type
            FROM dpi_raw dpi
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
    if PRINT_UNMATCHED_DIAGNOSTIC and unmatched_rows > 0:
        print("\nDiagnosing unmatched DPI rows by cause...")
        td = time.time()
        
        # Build set of all NAT keys (across full hour, no time bound)
        con.execute("""
            CREATE OR REPLACE TEMP TABLE nat_keys AS
            SELECT DISTINCT MSISDN, Private_IP, Private_IP_Port
            FROM read_csv(?, sep=?, header=True, ignore_errors=True)
        """, [syslog_path, INPUT_SEP])

        cause = con.execute(f"""
            WITH unmatched AS (
                SELECT d.MSISDN, d.Private_IP, d.Private_IP_Port, d.start_min
                FROM dpi_raw d
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
            'A_no_nat_record':  'A  No NAT record exists (any time)  → collector issue',
            'B_outside_window': 'B  NAT exists but outside time window → tuning needed',
        }
        for tag, n in cause:
            label = cause_labels.get(tag, tag)
            pct   = 100 * n / unmatched_rows
            print(f"  {label:<48}  {n:>14,}  ({pct:5.1f}%)")
        print(f"  {'-' * 56}")
        print(f"  Interpretation:")
        print(f"    A rows: Can only be resolved by fixing the NAT collector.")
        print(f"    B rows: Can be resolved by widening TIME_WINDOW_MINUTES.")

    con.close()

    # Sanity warnings
    if total_rows == 0:
        print("  [WARN] Output is empty — check input files and join keys")
    elif match_pct < 10.0:
        print(f"  [WARN] Match rate is very low ({match_pct:.1f}%) — check column names and separator")
    
    # Verify output size is reasonable (should equal DPI count)
    if total_rows != dpi_count:
        print(f"\n  ⚠️  WARNING: Output rows ({total_rows:,}) != DPI rows ({dpi_count:,})")
        print(f"      This indicates an issue with the QUALIFY clause or duplicate handling")

    total_time = time.time() - start_total
    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  DPI input rows:      {dpi_original_count:,}")
    print(f"  DPI unique rows:     {dpi_count:,}")
    print(f"  Output records:      {total_rows:,} (should equal DPI unique rows)")
    print(f"  Matched:             {matched_rows:,} ({match_pct:.2f}%)")
    print(f"  Unmatched:           {unmatched_rows:,}")
    print(f"  UDP probe rows:      {udp_rows:,}")
    print(f"  Total time:          {total_time:.2f}s ({total_time/60:.1f} min)")
    print(f"  Output file:         {output_path}")
    print("=" * 70)
    print("\n✅ PROCESSING SUMMARY:")
    print("   - Removed only PERFECT duplicate rows (100% identical)")
    print("   - Kept all rows that differ in ANY column")
    print("   - Used QUALIFY to pick closest NAT match per DPI row")
    print("   - No key-based deduplication (multiple NAT bindings preserved across different DPI rows)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user"); sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback; traceback.print_exc(); sys.exit(1)
