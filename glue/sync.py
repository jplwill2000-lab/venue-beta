"""Venue beta glue: poll Frigate events -> cases; export evidence packs to SeaweedFS on request."""
import os, time, io, zipfile, requests, psycopg

FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
FILES = os.environ.get("FILES_URL", "http://files:8888")
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
ALTER TABLE cases ADD COLUMN IF NOT EXISTS evidence_url text;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS exported_at timestamptz;
"""

def db():
    return psycopg.connect(
        host=os.environ["PGHOST"], port=os.environ.get("PGPORT", "5432"),
        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"], connect_timeout=10)

def put_file(path, data, ctype):
    r = requests.put(f"{FILES}/{path}", data=data,
                     headers={"Content-Type": ctype}, timeout=60)
    r.raise_for_status()

def do_exports(conn):
    rows = conn.execute(
        "SELECT id, frigate_event_id, camera, label, score, started_at FROM cases WHERE status='export_requested' ORDER BY id LIMIT 5"
    ).fetchall()
    for cid, eid, camera, label, score, started_at in rows:
        base = f"evidence/{eid}"
        try:
            meta = requests.get(f"{FRIGATE}/api/events/{eid}", timeout=10)
            meta.raise_for_status()
            put_file(f"{base}/metadata.json", meta.content, "application/json")
            parts = [("metadata.json", meta.content)]
            snap = requests.get(f"{FRIGATE}/api/events/{eid}/snapshot.jpg", timeout=15)
            if snap.status_code == 200 and snap.content:
                put_file(f"{base}/snapshot.jpg", snap.content, "image/jpeg")
                parts.append(("snapshot.jpg", snap.content))
            clip = requests.get(f"{FRIGATE}/api/events/{eid}/clip.mp4", timeout=30)
            if clip.status_code == 200 and clip.content:
                put_file(f"{base}/clip.mp4", clip.content, "video/mp4")
                parts.append(("clip.mp4", clip.content))
            summary = (
                f"Venue evidence pack\n"
                f"Case ID: {cid}\n"
                f"Frigate event: {eid}\n"
                f"Camera: {camera}\n"
                f"Detected: {label}\n"
                f"Confidence: {score}\n"
                f"Started at: {started_at}\n"
                f"Exported at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
                f"\nFiles: metadata.json (raw detection data), snapshot.jpg (still image)"
                + (", clip.mp4 (video clip)" if any(n == 'clip.mp4' for n, _ in parts) else " (no clip - recording disabled on demo camera)")
                + "\n"
            )
            parts.append(("case.txt", summary.encode()))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in parts:
                    z.writestr(name, data)
            put_file(f"{base}/evidence.zip", buf.getvalue(), "application/zip")
            conn.execute(
                "UPDATE cases SET status='exported', evidence_url=%s, exported_at=now() WHERE id=%s",
                (f"{base}/evidence.zip", cid))
            print(f"exported case {cid} -> {base}/evidence.zip", flush=True)
        except Exception as e:
            conn.execute("UPDATE cases SET status='export_failed' WHERE id=%s", (cid,))
            print(f"export FAILED case {cid}: {e}", flush=True)

def main():
    while True:
        try:
            with db() as conn:
                conn.execute(SCHEMA)
            break
        except Exception as e:
            print(f"waiting for db: {e}", flush=True)
            time.sleep(5)
    print("cases table ready", flush=True)
    while True:
        try:
            r = requests.get(f"{FRIGATE}/api/events",
                             params={"limit": 50, "has_snapshot": 1}, timeout=10)
            r.raise_for_status()
            with db() as conn:
                new = 0
                for e in r.json():
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
                do_exports(conn)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
