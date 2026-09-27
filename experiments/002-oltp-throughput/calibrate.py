"""Load a 2% slice into local PostgreSQL, measure bytes per unit of scale, print the S-scale estimate.
Run: E002_PG_DSN=... python3 calibrate.py"""
import os
import sys

import psycopg

import datagen as G
import schema as SC

FRACTION = 0.02
conn = psycopg.connect(os.environ["E002_PG_DSN"], autocommit=True)
sc = G.scaled(FRACTION)
for s in SC.drop_sql() + SC.ddl("pg"):
    conn.execute(s)
for t in SC.TABLES[:-1]:
    for _, a, b in G.chunks(t, sc, 2000):
        G.insert_chunk(conn, t, a, b, sc, "copy")
conn.execute("VACUUM ANALYZE")
sizes = {t: conn.execute("SELECT pg_total_relation_size(%s)", (t,)).fetchone()[0] for t in SC.TABLES}
total = sum(sizes.values())
for t, b in sizes.items():
    print(f"  {t}: {b / FRACTION / 2**30:.2f} GiB at S")
print(f"slice {FRACTION}: {total / 2**30:.3f} GiB -> S estimate {total / FRACTION / 2**30:.2f} GiB")
sys.exit(0)
