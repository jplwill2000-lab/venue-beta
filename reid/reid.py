"""Venue beta person re-identification (appearance-based, no faces).

Per-request: embeds the seed person crop + candidate crops from one incident
window, ranks by cosine similarity, discards all embeddings. Nothing persists:
no profiles, no cross-day store. OSNet via torchreid, CPU.
"""
import os, io, time, json, requests
from flask import Flask, jsonify, request

FRIGATE = os.environ.get("FRIGATE_URL", "http://frigate:5000")
FILES = os.environ.get("FILES_URL", "http://files:8888")
TOP_N = 10

app = Flask(__name__)
model = None
transform = None

def load_model():
    global model, transform
    import torch, torchreid
    from PIL import Image
    model = torchreid.models.build_model(name="osnet_x1_0", num_classes=1000, pretrained=False)
    torchreid.utils.load_pretrained_weights(model, "/app/weights/osnet_x1_0.pth")
    model.eval()
    import torchvision.transforms as T
    transform = T.Compose([
        T.Resize((256, 128)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    print("model loaded", flush=True)

def embed(img_bytes):
    import torch
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    with torch.no_grad():
        feat = model(transform(img).unsqueeze(0))
    return feat[0]

def get_crop(eid):
    r = requests.get(f"{FRIGATE}/api/events/{eid}/snapshot.jpg",
                     params={"crop": 1, "quality": 80}, timeout=15)
    return r.content if r.status_code == 200 and r.content else None

def put_thumb(eid, data):
    try:
        requests.put(f"{FILES}/sightings/{eid}.jpg", data=data,
                     headers={"Content-Type": "image/jpeg"}, timeout=20)
        return f"sightings/{eid}.jpg"
    except Exception:
        return None

@app.route("/health")
def health():
    return jsonify({"ok": True, "model": model is not None})

@app.route("/sightings")
def sightings():
    eid = request.args.get("event_id", "")
    window = int(request.args.get("window", "15")) * 60
    t0 = time.time()
    ev = requests.get(f"{FRIGATE}/api/events/{eid}", timeout=10).json()
    start = ev.get("start_time") or time.time()
    seed_crop = get_crop(eid)
    if not seed_crop:
        return jsonify({"error": "no snapshot for seed event"}), 404
    seed_feat = embed(seed_crop)
    r = requests.get(f"{FRIGATE}/api/events", params={
        "after": int(start - window), "before": int(start + window),
        "labels": "person", "limit": 200, "has_snapshot": 1}, timeout=15)
    out = []
    import torch
    for e in r.json():
        cid = e["id"]
        if cid == eid:
            continue
        crop = get_crop(cid)
        if not crop:
            continue
        sim = float(torch.nn.functional.cosine_similarity(seed_feat, embed(crop), dim=0))
        thumb = put_thumb(cid, crop)
        out.append({
            "event_id": cid,
            "camera": e.get("camera"),
            "start_time": e.get("start_time"),
            "similarity": round(sim, 4),
            "thumb": thumb,
        })
    out.sort(key=lambda x: -x["similarity"])
    print(f"sightings for {eid}: {len(out)} candidates in {time.time()-t0:.1f}s", flush=True)
    return jsonify({"seed": eid, "window_minutes": window // 60, "candidates": out[:TOP_N]})

if __name__ == "__main__":
    load_model()
    app.run(host="0.0.0.0", port=8090)
