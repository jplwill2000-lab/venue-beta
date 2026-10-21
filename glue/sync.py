"""Venue beta glue: poll Frigate events, upsert case records into Postgres."""
import os, time, requests, psycopg

FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
POLL_SECONDS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id bigserial PRIMARY KEY,
  frigate_event_id text UNIQUE NOT NULL,
  camera text NOT NULL,
  label text NOT NULL,
  score numeric,
  started_at timestamptz,
  has_snapshot boolean DEFAULT false,
  status text NOT NULL DEFAULT 'new',
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

def db():
    return psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"])

def main():
    while True:
        try:
            with db() as conn:
                conn.execute(SCHEMA)
        except Exception as e:
            print(f"waiting for db: {e}", flush=True)
            time.sleep(5)
            continue
        break
    print("cases table ready", flush=True)
    while True:
        try:
            r = requests.get(f"{FRIGATE}/api/events",
                             params={"limit": 50, "has_snapshot": 1}, timeout=10)
            r.raise_for_status()
            events = r.json()
            new = 0
            with db() as conn:
                for e in events:
                    cur = conn.execute(
                        """INSERT INTO cases (frigate_event_id, camera, label, score, started_at, has_snapshot)
                           VALUES (%s,%s,%s,%s,to_timestamp(%s),%s)
                           ON CONFLICT (frigate_event_id) DO NOTHING""",
                        (e["id"], e["camera"], e["label"],
                         (e.get("data") or {}).get("score"), e.get("start_time"),
                         bool(e.get("has_snapshot"))))
                    new += cur.rowcount
            if new:
                print(f"inserted {new} new case(s)", flush=True)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
