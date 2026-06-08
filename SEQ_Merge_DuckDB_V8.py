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



# ── TUNED FOR THIS SERVER (251 GB RAM / 53 TB disk / 248 GB free observed) ──

# Previous values '16GB' / 8 / '500GB' caused the temp-dir OOM at 465 GiB used.

#   * 16 GB on a 251 GB box forced spill-from-byte-one; spilled bytes are

#     2-4x their in-RAM size due to format overhead — that's what filled

#     the temp directory at 465 GiB.

#   * 500 GB temp cap was the actual ceiling we hit; you have 6.4 TB free.

#   * 8 threads holds 8 concurrent partitions of in-flight data in memory.

#     DuckDB's OOM message lists "reduce threads" as the first remedy.



DUCKDB_MEMORY_LIMIT  = '180GB'

DUCKDB_THREADS       = 4

DUCKDB_MAX_TEMP_SIZE = '4TB'



if platform.system() == 'Windows':

    DUCKDB_TEMP_DIR = r"D:/duckdb_tmp"

else:

    DUCKDB_TEMP_DIR = "/Data/home/SMARTCARE_FEEDS/INPUT/SEQ_Merged_IPv4/duckdb_tmp/"



INPUT_SEP           = '|'

OUTPUT_SEP          = '|'

TIME_WINDOW_MINUTES = 1

MIN_FILE_SIZE_MB    = 1

SAMPLE_ROWS         = None



# Safety: abort if free RAM (GB) is below this before starting DuckDB

MIN_FREE_RAM_GB     = 4

# Safety: abort if free disk on temp dir (GB) is below this

MIN_FREE_DISK_GB    = 20



# Drop zero-duration NAT rows (UDP probes) from the join. Last hour ~22% of

# NAT rows were probes (42M of 189M); diagnostic showed they almost never

# produce matches but bloat the intermediate set. Setting this False

# restores the original behaviour for A/B comparison.

DROP_UDP_PROBES_FROM_JOIN = True





# =========================================================

# PREFLIGHT CHECKS  — all run before DuckDB starts

# =========================================================



def check_ram():

    """Abort early if not enough free RAM to safely run the join."""

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

            f"need at least {MIN_FREE_RAM_GB} GB. "

            f"Kill other processes or reduce DUCKDB_MEMORY_LIMIT."

        )

    # Warn if memory_limit is set higher than available RAM

    limit_gb = float(DUCKDB_MEMORY_LIMIT.rstrip('GB').rstrip('gb'))

    if limit_gb > total_gb * 0.85:

        print(f"  [WARN] DUCKDB_MEMORY_LIMIT ({DUCKDB_MEMORY_LIMIT}) "

              f"is >85% of total RAM ({total_gb:.1f} GB) — risk of OOM")

    # Also warn the opposite direction: limit set far below available RAM

    # forces unnecessary spill (this is what hit us last time).

    if limit_gb < free_gb * 0.4 and free_gb > 32:

        print(f"  [WARN] DUCKDB_MEMORY_LIMIT ({DUCKDB_MEMORY_LIMIT}) is far below "

              f"available RAM ({free_gb:.1f} GB) — forces excess disk spill")





def check_disk(path):

    """Abort early if temp/output directory has insufficient free space."""

    try:

        usage = shutil.disk_usage(path)

        free_gb  = usage.free  / (1024 ** 3)

        total_gb = usage.total / (1024 ** 3)

    except Exception as e:

        print(f"  [WARN] Could not check disk on {path}: {e}")

        return

    print(f"  Disk ({path}): {free_gb:.1f} GB free / {total_gb:.1f} GB total")

    if free_gb < MIN_FREE_DISK_GB:

        raise RuntimeError(

            f"Not enough free disk on {path}: {free_gb:.1f} GB available, "

            f"need at least {MIN_FREE_DISK_GB} GB for temp spill + output."

        )





def check_temp_dir_writable(path):

    """Ensure DuckDB temp dir exists and is writable before we even start."""

    try:

        os.makedirs(path, exist_ok=True)

    except PermissionError:

        raise RuntimeError(f"Cannot create temp directory (permission denied): {path}")

    test_file = os.path.join(path, '.write_test')

    try:

        with open(test_file, 'w') as f:

            f.write('ok')

        os.remove(test_file)

    except Exception as e:

        raise RuntimeError(f"Temp directory is not writable: {path} — {e}")





def check_output_dir_writable(path):

    """Ensure output directory exists and is writable."""

    try:

        os.makedirs(path, exist_ok=True)

    except PermissionError:

        raise RuntimeError(f"Cannot create output directory (permission denied): {path}")

    test_file = os.path.join(path, '.write_test')

    try:

        with open(test_file, 'w') as f:

            f.write('ok')

        os.remove(test_file)

    except Exception as e:

        raise RuntimeError(f"Output directory is not writable: {path} — {e}")





def check_csv_columns(path, required_cols, label, sep='|'):

    """Read only the header row and verify required columns are present."""

    try:

        with open(path, 'r', errors='replace') as f:

            header = f.readline().rstrip('\n').split(sep)

        header_stripped = [c.strip() for c in header]

        missing = [c for c in required_cols if c not in header_stripped]

        if missing:

            raise RuntimeError(

                f"{label} file is missing columns: {missing}\n"

                f"  Found: {header_stripped}\n"

                f"  Path:  {path}"

            )

        print(f"  {label} columns OK ({len(header_stripped)} total)")

    except RuntimeError:

        raise

    except Exception as e:

        raise RuntimeError(f"Could not read header of {label} file: {path} — {e}")





def check_file_ready(path, label):

    size_mb = os.path.getsize(path) / (1024 * 1024)

    if size_mb < MIN_FILE_SIZE_MB:

        raise RuntimeError(

            f"{label} file looks incomplete: {path} "

            f"({size_mb:.2f} MB) — feed may still be writing, retry in a few minutes."

        )

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

    print("IPv4 MERGE - PRODUCTION (v7 — crash-safe, tuned for 251GB box)")

    print("=" * 70)

    print(f"  Time Window:  ±{TIME_WINDOW_MINUTES} minute(s)")

    print(f"  Memory Limit: {DUCKDB_MEMORY_LIMIT}")

    print(f"  Threads:      {DUCKDB_THREADS}")

    print(f"  Temp ceiling: {DUCKDB_MAX_TEMP_SIZE}")

    print(f"  Drop probes:  {DROP_UDP_PROBES_FROM_JOIN}")



    date, hour = DATE_HOUR_OVERRIDE if DATE_HOUR_OVERRIDE else get_current_datetime()

    print(f"\nTarget: {date} hour {hour}")



    # ── RESOLVE + EXISTENCE ──

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



    # Column presence check — catches separator/schema mismatches before DuckDB starts

    check_csv_columns(syslog_path, ['START_TIME', 'END_TIME', 'MSISDN', 'Private_IP',

                                     'Private_IP_Port', 'Public_NAT_IP', 'Public_NAT_IP_Port'],

                      'Syslog', sep=INPUT_SEP)

    check_csv_columns(ipv4_path,   ['START_TIME', 'MSISDN', 'Private_IP', 'Private_IP_Port'],

                      'IPv4',   sep=INPUT_SEP)



    # RAM check — abort before OOM, not during

    check_ram()



    # Temp dir: exists + writable + enough disk

    check_temp_dir_writable(DUCKDB_TEMP_DIR)

    check_disk(DUCKDB_TEMP_DIR)



    # Output dir: exists + writable

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



    # Verify settings actually applied

    actual = con.execute("""

        SELECT current_setting('memory_limit'),

               current_setting('threads'),

               current_setting('temp_directory')

    """).fetchone()

    print(f"  DuckDB ready — memory={actual[0]}, threads={actual[1]}, "

          f"tmp={actual[2] or '(not set)'}  ({time.time()-t0:.2f}s)")



    sample_clause = f"LIMIT {SAMPLE_ROWS}" if SAMPLE_ROWS else ""

    win = TIME_WINDOW_MINUTES



    # Probe filter: drop zero-duration rows from the NAT side before the join.

    # This is the SINGLE most effective intermediate-size reduction:

    #   last production hour, 42M of 189M NAT rows were zero-duration.

    # Setting DROP_UDP_PROBES_FROM_JOIN=False restores the original behaviour.

    probe_filter = "WHERE END_TIME != START_TIME" if DROP_UDP_PROBES_FROM_JOIN else ""



    # ── SINGLE PIPELINE ──

    print("\nRunning join pipeline...")

    t1 = time.time()



    con.execute(f"""

        COPY (

            WITH

            nat AS (

                SELECT DISTINCT ON (

                        (CAST(START_TIME AS BIGINT) // 60)::INTEGER,

                        MSISDN, Private_IP, Private_IP_Port

                    )

                    (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min,

                    MSISDN,

                    Private_IP,

                    Private_IP_Port,

                    Public_NAT_IP,

                    Public_NAT_IP_Port,

                    CASE WHEN END_TIME = START_TIME

                         THEN 'udp_probe' ELSE 'session' END AS session_type

                FROM read_csv('{syslog_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)

                {probe_filter}

                {sample_clause}

            ),

            dpi AS (

                SELECT *,

                    (CAST(START_TIME AS BIGINT) // 60)::INTEGER AS start_min

                FROM read_csv('{ipv4_path}', sep='{INPUT_SEP}', header=True, ignore_errors=True)

                {sample_clause}

            )

            SELECT

                dpi.* EXCLUDE (start_min),

                nat.Public_NAT_IP,

                nat.Public_NAT_IP_Port,

                nat.session_type

            FROM dpi

            LEFT JOIN nat

                ON  nat.MSISDN          = dpi.MSISDN

                AND nat.Private_IP      = dpi.Private_IP

                AND nat.Private_IP_Port = dpi.Private_IP_Port

                AND nat.start_min BETWEEN (dpi.start_min - {win})

                                      AND (dpi.start_min + {win})

        )

        TO '{output_path}' (HEADER TRUE, DELIMITER '{OUTPUT_SEP}')

    """)

    print(f"  Pipeline completed ({time.time()-t1:.2f}s)")



    # ── STATS ──

    print("\nComputing stats...")

    stats = con.execute(f"""

        SELECT

            COUNT(*)                                                                    AS total_rows,

            COALESCE(SUM(CASE WHEN Public_NAT_IP IS NOT NULL

                              AND Public_NAT_IP != '' THEN 1 ELSE 0 END), 0)           AS matched_rows,

            COALESCE(SUM(CASE WHEN session_type = 'udp_probe' THEN 1 ELSE 0 END), 0)   AS udp_rows

        FROM read_csv('{output_path}', sep='{OUTPUT_SEP}', header=True, ignore_errors=True)

    """).fetchone()



    con.close()



    total_rows, matched_rows, udp_rows = stats

    unmatched_rows = total_rows - matched_rows

    match_pct      = (matched_rows / total_rows * 100) if total_rows else 0.0



    # Sanity check — alert if match rate looks wrong

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

        import traceback; traceback.print_exc(); sys.exit(1)        syslog_path, ipv4_path, resolved_hour = resolve_input_files(
            INPUT_SYSLOG_DIR, INPUT_IPV4_DIR, SYSLOG_PATTERN, IPV4_PATTERN, date, hour
        )
    else:
        syslog_path, ipv4_path, resolved_hour = manual_resolve_input_files(
            INPUT_SYSLOG_DIR, INPUT_IPV4_DIR, SYSLOG_PATTERN, IPV4_PATTERN, date, hour
        )
    
    # Validate files exist
    for file_path, file_type in [(syslog_path, "Syslog"), (ipv4_path, "IPv4")]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"{file_type} file not found: {file_path}")
    
    print(f"  ✓ Syslog file: {syslog_path}")
    print(f"  ✓ IPv4 file:   {ipv4_path}")
    
    # Create output directory
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_name = f'SEQ_IPV4_Merge_{date}_{resolved_hour}.csv'
    output_path = os.path.join(OUTPUT_DIR, out_name)
    print(f"  ✓ Output file: {output_path}")
    
    # =========================================================
    # STEP 1: INITIALIZE DUCKDB WITH TUNING
    # =========================================================
    print("\n[1/6] Initializing DuckDB with performance tuning...")
    t1 = time.time()
    
    con = duckdb.connect(database=':memory:')
    
    # Apply all tuning levers
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    
    if DUCKDB_TEMP_DIR:
        os.makedirs(DUCKDB_TEMP_DIR, exist_ok=True)
        con.execute(f"SET temp_directory='{DUCKDB_TEMP_DIR.replace('\\', '/')}'")
    
    if DUCKDB_MAX_TEMP_SIZE:
        con.execute(f"SET max_temp_directory_size='{DUCKDB_MAX_TEMP_SIZE}'")
    
    con.execute(f"SET preserve_insertion_order={str(DUCKDB_PRESERVE_ORDER).lower()}")
    
    if DUCKDB_PROGRESS_BAR:
        con.execute("SET enable_progress_bar=true")
    
    print(f"  ✓ DuckDB ready ({time.time() - t1:.2f}s)")
    
    # =========================================================
    # STEP 2: ANALYZE DATA OVERLAP BEFORE JOIN (Diagnostic)
    # =========================================================
    print("\n[2/6] Analyzing data overlap between files...")
    t1 = time.time()
    
    sample_clause = f"LIMIT {SAMPLE_ROWS}" if SAMPLE_ROWS else ""
    
    # Get unique keys from both files
    overlap_query = f"""
        WITH 
        syslog_keys AS (
            SELECT DISTINCT
                (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER AS START_TIME_MIN,
                TRIM("MSISDN") AS MSISDN,
                TRIM("Private_IP") AS Private_IP,
                TRIM("Private_IP_Port") AS Private_IP_Port
            FROM read_csv_auto('{syslog_path}', sep='{INPUT_SEP}', header=True, all_varchar=True, ignore_errors=True)
            {sample_clause}
        ),
        ipv4_keys AS (
            SELECT DISTINCT
                (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER AS START_TIME_MIN,
                TRIM("MSISDN") AS MSISDN,
                TRIM("Private_IP") AS Private_IP,
                TRIM("Private_IP_Port") AS Private_IP_Port
            FROM read_csv_auto('{ipv4_path}', sep='{INPUT_SEP}', header=True, all_varchar=True, ignore_errors=True)
            {sample_clause}
        )
        SELECT 
            (SELECT COUNT(*) FROM syslog_keys) AS syslog_unique_keys,
            (SELECT COUNT(*) FROM ipv4_keys) AS ipv4_unique_keys,
            (SELECT COUNT(*) FROM syslog_keys s WHERE EXISTS 
                (SELECT 1 FROM ipv4_keys i 
                 WHERE i.START_TIME_MIN = s.START_TIME_MIN 
                   AND i.MSISDN = s.MSISDN 
                   AND i.Private_IP = s.Private_IP 
                   AND i.Private_IP_Port = s.Private_IP_Port)
            ) AS exact_match_keys,
            (SELECT COUNT(*) FROM syslog_keys s WHERE EXISTS 
                (SELECT 1 FROM ipv4_keys i 
                 WHERE i.START_TIME_MIN BETWEEN (s.START_TIME_MIN - {TIME_WINDOW_TOLERANCE}) 
                                           AND (s.START_TIME_MIN + {TIME_WINDOW_TOLERANCE})
                   AND i.MSISDN = s.MSISDN 
                   AND i.Private_IP = s.Private_IP 
                   AND i.Private_IP_Port = s.Private_IP_Port)
            ) AS window_match_keys
    """
    
    overlap_stats = con.execute(overlap_query).fetchone()
    syslog_keys, ipv4_keys, exact_match, window_match = overlap_stats
    
    print(f"  Syslog unique keys:     {syslog_keys:,}")
    print(f"  IPv4 unique keys:       {ipv4_keys:,}")
    print(f"  Exact minute matches:   {exact_match:,} ({exact_match/syslog_keys*100:.1f}% of syslog)" if syslog_keys > 0 else "  Exact minute matches:   0")
    print(f"  Window matches (±{TIME_WINDOW_TOLERANCE} min): {window_match:,} ({window_match/syslog_keys*100:.1f}% of syslog)" if syslog_keys > 0 else "  Window matches:         0")
    
    # Calculate potential improvement from sliding window
    if exact_match > 0:
        improvement = ((window_match - exact_match) / exact_match) * 100
        print(f"  Sliding window improvement: +{improvement:.1f}% more matches")
    
    print(f"  Analysis time:          {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 3: PROCESS SYSLOG FILE (FILE 1)
    # =========================================================
    print("\n[3/6] Processing Syslog file (FILE 1)...")
    t1 = time.time()
    
    file1_query = f"""
        CREATE OR REPLACE TEMP TABLE f1_distinct AS 
        SELECT DISTINCT
            (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER AS START_TIME_MIN,
            TRIM("MSISDN") AS MSISDN,
            TRIM("Private_IP") AS Private_IP,
            TRIM("Private_IP_Port") AS Private_IP_Port,
            TRIM("Public_NAT_IP") AS Public_NAT_IP,
            TRIM("Public_NAT_IP_Port") AS Public_NAT_IP_Port
        FROM read_csv_auto('{syslog_path}', 
                           sep='{INPUT_SEP}', 
                           header=True, 
                           all_varchar=True,
                           ignore_errors=True)
        {sample_clause}
    """
    con.execute(file1_query)
    f1_count = con.execute("SELECT COUNT(*) FROM f1_distinct;").fetchone()[0]
    print(f"  ✓ Syslog unique groupings: {f1_count:,}")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 4: PROCESS IPV4 FILE (FILE 2)
    # =========================================================
    print("\n[4/6] Processing IPv4 file (FILE 2)...")
    t1 = time.time()
    
    file2_query = f"""
        CREATE OR REPLACE TEMP TABLE f2_distinct AS
        SELECT * FROM (
            SELECT *,
                (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER AS START_TIME_MIN,
                ROW_NUMBER() OVER(
                    PARTITION BY 
                        (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER, 
                        TRIM("MSISDN"), 
                        TRIM("Private_IP"), 
                        TRIM("Private_IP_Port")
                ) as row_num
            FROM read_csv_auto('{ipv4_path}', 
                               sep='{INPUT_SEP}', 
                               header=True, 
                               all_varchar=True,
                               ignore_errors=True)
            {sample_clause}
        ) WHERE row_num = 1
    """
    con.execute(file2_query)
    f2_count = con.execute("SELECT COUNT(*) FROM f2_distinct;").fetchone()[0]
    print(f"  ✓ IPv4 unique sessions: {f2_count:,}")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 5: SLIDING MINUTE WINDOW JOIN (CRITICAL ENHANCEMENT)
    # =========================================================
    print(f"\n[5/6] Performing LEFT JOIN with ±{TIME_WINDOW_TOLERANCE} minute sliding window...")
    t1 = time.time()
    
    # This is the KEY ENHANCEMENT from your script
    # Uses BETWEEN clause to capture traffic that crosses minute boundaries
    merge_query = f"""
        CREATE OR REPLACE TEMP TABLE merged_output_raw AS
        SELECT 
            f2.* EXCLUDE (row_num, START_TIME_MIN), 
            f1.Public_NAT_IP,
            f1.Public_NAT_IP_Port,
            -- Rank matches by closest time (smallest time difference)
            ROW_NUMBER() OVER(
                PARTITION BY f2.START_TIME, f2.MSISDN, f2.Private_IP, f2.Private_IP_Port
                ORDER BY ABS(f2.START_TIME_MIN - f1.START_TIME_MIN) ASC
            ) as time_rank
        FROM f2_distinct f2
        LEFT JOIN f1_distinct f1 ON 
            f1.START_TIME_MIN BETWEEN (f2.START_TIME_MIN - {TIME_WINDOW_TOLERANCE}) 
                                  AND (f2.START_TIME_MIN + {TIME_WINDOW_TOLERANCE}) AND
            TRIM(f2.MSISDN)          = f1.MSISDN AND
            TRIM(f2.Private_IP)      = f1.Private_IP AND
            TRIM(f2.Private_IP_Port) = f1.Private_IP_Port
    """
    con.execute(merge_query)
    
    # Select only the best time match for each record (closest minute)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE merged_output AS 
        SELECT * EXCLUDE(time_rank) FROM merged_output_raw WHERE time_rank = 1
    """)
    
    print(f"  ✓ Join completed with sliding time window")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 6: OUTPUT WITH CORRECT STATISTICS
    # =========================================================
    print("\n[6/6] Generating output and statistics...")
    t1 = time.time()
    
    # Add match tracking (similar to earlier fix)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE merged_output_with_flag AS
        SELECT 
            *,
            CASE 
                WHEN Public_NAT_IP IS NOT NULL AND Public_NAT_IP != '' 
                THEN 1 
                ELSE 0 
            END AS is_matched
        FROM merged_output
    """)
    
    # Stream to CSV
    con.execute(f"""
        COPY (SELECT * EXCLUDE (is_matched) FROM merged_output_with_flag) 
        TO '{output_path}' 
        (HEADER TRUE, DELIMITER '{OUTPUT_SEP}');
    """)
    
    # Calculate correct statistics
    stats = con.execute("""
        SELECT 
            COUNT(*) AS total_rows,
            SUM(is_matched) AS matched_rows,
            COUNT(*) - SUM(is_matched) AS unmatched_rows
        FROM merged_output_with_flag
    """).fetchone()
    total_rows, matched_rows, unmatched_rows = stats
    
    print(f"\n  ✓ Output file generated: {output_path}")
    print(f"\n  {'=' * 50}")
    print(f"  MERGE STATISTICS (with ±{TIME_WINDOW_TOLERANCE} min window)")
    print(f"  {'=' * 50}")
    print(f"  Total IPv4 Records:     {total_rows:,}")
    print(f"  Matched to Syslog:      {matched_rows:,}")
    print(f"  Unmatched Records:      {unmatched_rows:,}")
    
    if total_rows > 0:
        match_percentage = (matched_rows / total_rows) * 100
        print(f"  Match Rate:             {match_percentage:.2f}%")
        
        # Compare with exact match expectation
        if syslog_keys > 0 and exact_match > 0:
            expected_exact_rate = (exact_match / min(syslog_keys, ipv4_keys)) * 100 if min(syslog_keys, ipv4_keys) > 0 else 0
            print(f"\n  Time Window Impact:")
            print(f"    Expected exact match rate: {expected_exact_rate:.1f}%")
            print(f"    Actual sliding window rate: {match_percentage:.1f}%")
            improvement = match_percentage - expected_exact_rate
            if improvement > 0:
                print(f"    Improvement: +{improvement:.1f}% from ±{TIME_WINDOW_TOLERANCE} min window")
    
    print(f"\n  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # FINAL SUMMARY
    # =========================================================
    total_time = time.time() - start_total
    
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETED SUCCESSFULLY")
    print("=" * 80)
    print(f"  Input Summary:")
    print(f"    Syslog unique keys:     {syslog_keys:,}")
    print(f"    IPv4 unique keys:       {ipv4_keys:,}")
    print(f"    Exact key matches:      {exact_match:,}")
    print(f"    Window key matches:     {window_match:,}")
    print(f"  Output Summary:")
    print(f"    Total records:          {total_rows:,}")
    print(f"    Matched records:        {matched_rows:,} ({match_percentage:.1f}%)" if total_rows > 0 else "    Matched records:        0")
    print(f"    Unmatched records:      {unmatched_rows:,}")
    print(f"  Performance:")
    print(f"    Total time:             {total_time:.2f} seconds")
    print(f"    Output file:            {output_path}")
    print(f"    Time window:            ±{TIME_WINDOW_TOLERANCE} minute(s)")
    print("=" * 80 + "\n")
    
    con.close()
    
    return match_percentage if total_rows > 0 else 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
