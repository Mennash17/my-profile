import sys
import time
from pathlib import Path
import duckdb

# =========================================================
# VALIDATE ARGUMENTS
# =========================================================

if len(sys.argv) != 4:
    print("\nUsage:")
    print("python3.11 FR4.py <FILE_1> <FILE_2> <OUTPUT_FILE>\n")
    sys.exit(1)

FILE_1 = sys.argv[1]
FILE_2 = sys.argv[2]
OUTPUT_FILE = sys.argv[3]

# =========================================================
# TIMER & CONFIG
# =========================================================
start_total = time.time()
SEPARATOR = "|"

print("=" * 80)
print("DUCKDB ULTRA-PERFORMANCE ENHANCED IPv4 MERGE")
print("=" * 80)

# Validate Files
for f in [FILE_1, FILE_2]:
    if not Path(f).exists():
        raise FileNotFoundError(f"File not found: {f}")

print(f"\n✓ Found File 1: {FILE_1}")
print(f"✓ Found File 2: {FILE_2}")

# Initialize in-memory DuckDB session
con = duckdb.connect(database=':memory:')

# Optimize DuckDB memory usage limits if your server is restricted
# con.execute("SET max_memory='32GB';") 

# =========================================================
# STEP 1: LAZY-LOAD & DEDUPLICATE FILE 1
# =========================================================
print("\n[1/5] Extracting & Deduplicating FILE 1...")
t1 = time.time()

# DuckDB's read_csv_auto scans headers and types dynamically. 
# We explicitly cast and strip text columns directly inside the SQL engine.
file1_query = f"""
    CREATE OR REPLACE TEMP TABLE f1_distinct AS 
    SELECT DISTINCT
        (TRY_CAST(TRIM("START_TIME") AS BIGINT) // 60)::INTEGER AS START_TIME_MIN,
        TRIM("MSISDN") AS MSISDN,
        TRIM("Private_IP") AS Private_IP,
        TRIM("Private_IP_Port") AS Private_IP_Port,
        TRIM("Public_NAT_IP") AS Public_NAT_IP,
        TRIM("Public_NAT_IP_Port") AS Public_NAT_IP_Port
    FROM read_csv_auto('{FILE_1}', sep='{SEPARATOR}', header=True, all_varchar=True);
"""
con.execute(file1_query)
f1_count = con.execute("SELECT COUNT(*) FROM f1_distinct;").fetchone()[0]
print(f"✓ FILE 1 Processed (Unique Groupings): {f1_count:,} records")
print(f"Time: {time.time() - t1:.2f} sec")

# =========================================================
# STEP 2: LAZY-LOAD & DEDUPLICATE FILE 2
# =========================================================
print("\n[2/5] Extracting & Deduplicating FILE 2...")
t1 = time.time()

# We isolate the specific keys to drop duplicates, but carry all other remaining columns (*)
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
        FROM read_csv_auto('{FILE_2}', sep='{SEPARATOR}', header=True, all_varchar=True)
    ) WHERE row_num = 1;
"""
con.execute(file2_query)
f2_count = con.execute("SELECT COUNT(*) FROM f2_distinct;").fetchone()[0]
print(f"✓ FILE 2 Processed (Unique Sessions): {f2_count:,} records")
print(f"Time: {time.time() - t1:.2f} sec")

# =========================================================
# STEP 3: HIGH-SPEED CORRELATION MERGE
# =========================================================
print("\n[3/5] Correlating IP addresses (Left Join)...")
t1 = time.time()

merge_query = """
    CREATE OR REPLACE TEMP TABLE merged_output AS
    SELECT 
        f2.* EXCLUDE (row_num, START_TIME_MIN), -- Drop helper variables dynamically
        f1.Public_NAT_IP,
        f1.Public_NAT_IP_Port
    FROM f2_distinct f2
    LEFT JOIN f1_distinct f1 ON 
        f2.START_TIME_MIN  = f1.START_TIME_MIN AND
        TRIM(f2.MSISDN)     = f1.MSISDN AND
        TRIM(f2.Private_IP)  = f1.Private_IP AND
        TRIM(f2.Private_IP_Port) = f1.Private_IP_Port;
"""
con.execute(merge_query)
print("✓ Core table join successfully built in memory.")
print(f"Time: {time.time() - t1:.2f} sec")

# =========================================================
# STEP 4: STATISTICS CALCULATIONS
# =========================================================
print("\n[4/5] Computing Match Statistics...")
t1 = time.time()

total_rows = con.execute("SELECT COUNT(*) FROM merged_output;").fetchone()[0]
matched_rows = con.execute("SELECT COUNT(*) FROM merged_output WHERE Public_NAT_IP IS NOT NULL;").fetchone()[0]
unmatched_rows = total_rows - matched_rows

print("\nMerge Statistics")
print("-" * 50)
print(f"Total Output Rows  : {total_rows:,}")
print(f"Matched Rows       : {matched_rows:,}")
print(f"Unmatched Rows     : {unmatched_rows:,}")
if total_rows > 0:
    print(f"Match %            : {(matched_rows / total_rows) * 100:.2f}%")
print(f"Statistics Calculated in: {time.time() - t1:.2f} sec")

# =========================================================
# STEP 5: STREAM DIRECTLY TO COMPRESSED OUTPUT FILE
# =========================================================
print("\n[5/5] Streaming raw relational table straight to Disk...")
t1 = time.time()

# Streams data cleanly without allocating separate large output buffers
con.execute(f"""
    COPY merged_output TO '{OUTPUT_FILE}' (HEADER TRUE, DELIMITER '{SEPARATOR}');
""")

print(f"✓ Target file securely generated: {OUTPUT_FILE}")
print(f"Save Time: {time.time() - t1:.2f} sec")

# =========================================================
# FINAL WRAP UP
# =========================================================
print("\n" + "=" * 80)
print("METRIC DATA EXTRACTION COMPLETED SUCCESSFULLY")
print("=" * 80)
print(f"Total Execution Time: {time.time() - start_total:.2f} sec\n")