
import sys
import time
import os
from pathlib import Path
import duckdb
from datetime import datetime

# Try to import seq_common for dynamic file finding
try:
    import seq_common as common
    from seq_common import resolve_input_files
    HAS_SEQ_COMMON = True
except ImportError:
    HAS_SEQ_COMMON = False
    print("Warning: seq_common not found, using manual file resolution")

# =========================================================
# CONFIGURATION
# =========================================================

# File patterns (for dynamic file finding)
INPUT_SYSLOG_DIR = 'SEQ_IPv4'
INPUT_IPV4_DIR = 'SEQ_IPv4_NEW'
OUTPUT_DIR = 'OUTPUT'
SYSLOG_PATTERN = 'SEQ_IPv4_{date}_{hour}.csv'
IPV4_PATTERN = 'SEQ_IPV4_NEW_{date}_{hour}.csv'
DATE_HOUR_OVERRIDE = None  # Set to ('20260517', '08') to override

# DuckDB performance tuning
DUCKDB_MEMORY_LIMIT = '20GB'
DUCKDB_THREADS = 4
DUCKDB_TEMP_DIR = r"D:/duckdb_tmp"
DUCKDB_MAX_TEMP_SIZE = '500GB'
DUCKDB_PRESERVE_ORDER = False
DUCKDB_PROGRESS_BAR = True

# Processing options
SAMPLE_ROWS = 1000000  # Set for testing (e.g., 10000000)
INPUT_SEP = '|'
OUTPUT_SEP = '|'

# NEW: Time window tolerance (minutes)
# CRITICAL ENHANCEMENT: Allows matching traffic that crosses minute boundaries
TIME_WINDOW_TOLERANCE = 1  # ±1 minute (can be adjusted to 0, 1, 2, etc.)


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def get_current_datetime():
    """Get current date and hour for file naming."""
    now = datetime.now()
    return now.strftime('%Y%m%d'), now.strftime('%H')

def manual_resolve_input_files(syslog_dir, ipv4_dir, syslog_pattern, ipv4_pattern, date, hour):
    """Manually resolve input files without seq_common."""
    syslog_file = os.path.join(syslog_dir, syslog_pattern.format(date=date, hour=hour))
    ipv4_file = os.path.join(ipv4_dir, ipv4_pattern.format(date=date, hour=hour))
    
    # Try with zero-padded hour
    if not os.path.exists(syslog_file):
        hour_int = int(hour)
        syslog_file = os.path.join(syslog_dir, syslog_pattern.format(date=date, hour=f"{hour_int:02d}"))
        ipv4_file = os.path.join(ipv4_dir, ipv4_pattern.format(date=date, hour=f"{hour_int:02d}"))
    
    return syslog_file, ipv4_file, hour


# =========================================================
# MAIN PROCESSING FUNCTION
# =========================================================

def main():
    start_total = time.time()
    
    print("=" * 80)
    print("ENHANCED HYBRID IPv4 MERGE - WITH SLIDING TIME WINDOW")
    print("=" * 80)
    print(f"  Time Window Tolerance: ±{TIME_WINDOW_TOLERANCE} minute(s)")
    print(f"  DuckDB Version:        {duckdb.__version__}")
    print(f"  Memory Limit:          {DUCKDB_MEMORY_LIMIT}")
    print(f"  Threads:               {DUCKDB_THREADS}")
    print(f"  Temp Directory:        {DUCKDB_TEMP_DIR}")
    
    # =========================================================
    # STEP 0: DYNAMIC FILE RESOLUTION
    # =========================================================
    print("\n[0/6] Resolving input files...")
    
    if DATE_HOUR_OVERRIDE:
        date, hour = DATE_HOUR_OVERRIDE
    else:
        if HAS_SEQ_COMMON:
            date, hour = common.get_current_datetime()
        else:
            date, hour = get_current_datetime()
    
    print(f"  Target date:  {date}")
    print(f"  Target hour:  {hour}")
    
    # Resolve file paths
    if HAS_SEQ_COMMON:
        syslog_path, ipv4_path, resolved_hour = resolve_input_files(
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
