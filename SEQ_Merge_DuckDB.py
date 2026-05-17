

import sys
import time
import os
from pathlib import Path
import duckdb
from datetime import datetime

# Try to import seq_common, fall back if not available
try:
    import seq_common as common
    from seq_common import resolve_input_files, detect_time_format
    HAS_SEQ_COMMON = True
except ImportError:
    HAS_SEQ_COMMON = False
    print("Warning: seq_common not found, using manual file resolution")

# =========================================================
# CONFIGURATION
# =========================================================

# --- File patterns (from seq_solution_3) ---
INPUT_SYSLOG_DIR = 'SEQ_IPv4'
INPUT_IPV4_DIR = 'SEQ_IPv4_NEW'
OUTPUT_DIR = 'OUTPUT'
SYSLOG_PATTERN = 'SEQ_IPv4_{date}_{hour}.csv'
IPV4_PATTERN = 'SEQ_IPV4_NEW_{date}_{hour}.csv'
DATE_HOUR_OVERRIDE = None  # Set to ('20260517', '08') to override

# --- DuckDB performance tuning (from my tuned script) ---
DUCKDB_MEMORY_LIMIT = '20GB'
DUCKDB_THREADS = 4
DUCKDB_TEMP_DIR = r"D:/duckdb_tmp"
DUCKDB_MAX_TEMP_SIZE = '500GB'
DUCKDB_PRESERVE_ORDER = False
DUCKDB_PROGRESS_BAR = True

# --- Processing options ---
SAMPLE_ROWS = None  # Set to e.g., 10000000 for testing
INPUT_SEP = '|'
OUTPUT_SEP = '|'


# =========================================================
# HELPER FUNCTIONS (fallbacks if seq_common unavailable)
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
    print("ENHANCED HYBRID IPv4 MERGE")
    print("=" * 80)
    print(f"  Logic:        Your efficient dedup + integer timestamp")
    print(f"  Tuning:       Memory limits + temp directory + thread control")
    print(f"  DuckDB:       {duckdb.__version__}")
    print(f"  Input sep:    '{INPUT_SEP}'")
    print(f"  Output sep:   '{OUTPUT_SEP}'")
    print(f"  Temp dir:     {DUCKDB_TEMP_DIR}")
    print(f"  Memory limit: {DUCKDB_MEMORY_LIMIT}")
    print(f"  Threads:      {DUCKDB_THREADS}")
    
    # =========================================================
    # STEP 0: DYNAMIC FILE RESOLUTION
    # =========================================================
    print("\n[0/5] Resolving input files...")
    
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
    print("\n[1/5] Initializing DuckDB with performance tuning...")
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
    
    # Show applied settings
    settings = [
        f"  memory_limit = {DUCKDB_MEMORY_LIMIT}",
        f"  threads = {DUCKDB_THREADS}",
        f"  temp_directory = {DUCKDB_TEMP_DIR}",
        f"  max_temp_directory_size = {DUCKDB_MAX_TEMP_SIZE}",
        f"  preserve_insertion_order = {DUCKDB_PRESERVE_ORDER}",
    ]
    for setting in settings:
        print(setting)
    
    print(f"  ✓ DuckDB ready ({time.time() - t1:.2f}s)")
    
    # =========================================================
    # STEP 2: PROCESS FILE 1 (SYSLOG) WITH DEDUPLICATION
    # =========================================================
    print("\n[2/5] Processing Syslog file (DISTINCT deduplication)...")
    t1 = time.time()
    
    # Add sample limit if specified
    sample_clause = f"LIMIT {SAMPLE_ROWS}" if SAMPLE_ROWS else ""
    
    # Your efficient integer-based timestamp truncation
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
    print(f"  ✓ Syslog records (unique groupings): {f1_count:,}")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 3: PROCESS FILE 2 (IPV4) WITH DEDUPLICATION
    # =========================================================
    print("\n[3/5] Processing IPv4 file (ROW_NUMBER deduplication)...")
    t1 = time.time()
    
    # Your ROW_NUMBER() deduplication logic
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
    print(f"  ✓ IPv4 records (unique sessions): {f2_count:,}")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 4: PERFORM LEFT JOIN MERGE
    # =========================================================
    print("\n[4/5] Performing LEFT JOIN correlation...")
    t1 = time.time()
    
    merge_query = """
        CREATE OR REPLACE TEMP TABLE merged_output AS
        SELECT 
            f2.* EXCLUDE (row_num, START_TIME_MIN),
            f1.Public_NAT_IP,
            f1.Public_NAT_IP_Port
        FROM f2_distinct f2
        LEFT JOIN f1_distinct f1 ON 
            f2.START_TIME_MIN = f1.START_TIME_MIN AND
            TRIM(f2.MSISDN) = f1.MSISDN AND
            TRIM(f2.Private_IP) = f1.Private_IP AND
            TRIM(f2.Private_IP_Port) = f1.Private_IP_Port
    """
    con.execute(merge_query)
    
    print(f"  ✓ Join completed in memory")
    print(f"  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # STEP 5: STREAM TO OUTPUT & CALCULATE STATISTICS
    # =========================================================
    print("\n[5/5] Streaming to output file and calculating statistics...")
    t1 = time.time()
    
    # Stream to CSV file
    con.execute(f"""
        COPY merged_output TO '{output_path}' 
        (HEADER TRUE, DELIMITER '{OUTPUT_SEP}');
    """)
    
    # Calculate statistics
    stats_query = """
        SELECT 
            COUNT(*) AS total,
            COUNT(Public_NAT_IP) AS matched
        FROM merged_output
        WHERE Public_NAT_IP IS NOT NULL AND Public_NAT_IP != ''
    """
    stats = con.execute(stats_query).fetchone()
    total_rows, matched_rows = stats[0], stats[1]
    unmatched_rows = total_rows - matched_rows
    
    print(f"\n  ✓ Output file generated: {output_path}")
    print(f"\n  Merge Statistics")
    print(f"  {'-' * 50}")
    print(f"  Total Output Rows  : {total_rows:,}")
    print(f"  Matched Rows       : {matched_rows:,}")
    print(f"  Unmatched Rows     : {unmatched_rows:,}")
    if total_rows > 0:
        print(f"  Match Percentage   : {(matched_rows / total_rows) * 100:.2f}%")
    print(f"\n  Time: {time.time() - t1:.2f}s")
    
    # =========================================================
    # FINAL SUMMARY
    # =========================================================
    total_time = time.time() - start_total
    
    print("\n" + "=" * 80)
    print("PROCESSING COMPLETED SUCCESSFULLY")
    print("=" * 80)
    print(f"  Input Syslog:     {f1_count:,} unique groupings")
    print(f"  Input IPv4:       {f2_count:,} unique sessions")
    print(f"  Output Records:   {total_rows:,}")
    print(f"  Match Rate:       {(matched_rows/total_rows*100):.2f}%" if total_rows > 0 else "  Match Rate:       N/A")
    print(f"  Total Time:       {total_time:.2f} seconds")
    print(f"  Output File:      {output_path}")
    print(f"  Temp Directory:   {DUCKDB_TEMP_DIR}")
    
    # Show temp directory usage if available
    if DUCKDB_TEMP_DIR and os.path.exists(DUCKDB_TEMP_DIR):
        try:
            temp_size = sum(os.path.getsize(os.path.join(DUCKDB_TEMP_DIR, f)) 
                          for f in os.listdir(DUCKDB_TEMP_DIR) 
                          if os.path.isfile(os.path.join(DUCKDB_TEMP_DIR, f)))
            if temp_size > 0:
                print(f"  Temp Space Used:  {temp_size / (1024**3):.2f} GB")
        except:
            pass
    
    print("=" * 80 + "\n")
    
    con.close()


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
