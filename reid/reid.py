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

def get_frame(eid, boxed=False):
    params = {"quality": 85}
    if boxed:
        params["bbox"] = 1
    else:
        params["bbox"] = 0
        params["timestamp"] = 0
    r = requests.get(f"{FRIGATE}/api/events/{eid}/snapshot.jpg",
                     params=params, timeout=15)
    return r.content if r.status_code == 200 and r.content else None

def crop_to_box(img_bytes, box, margin=0.15):
    """box: normalized [x, y, w, h]. Returns cropped image bytes (JPEG)."""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = img.size
    x, y, w, h = box
    mx, my = w * margin, h * margin
    left = max(0, int((x - mx) * W)); top = max(0, int((y - my) * H))
    right = min(W, int((x + w + mx) * W)); bottom = min(H, int((y + h + my) * H))
    if right - left < 10 or bottom - top < 10:
        return img_bytes
    out = io.BytesIO()
    img.crop((left, top, right, bottom)).save(out, "JPEG", quality=85)
    return out.getvalue()

def make_crop(img_bytes, box, margin=0.5, min_h=480):
    """Padded person crop, upscaled so a small distant person is easier to see."""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = img.size
    x, y, w, h = box
    left = max(0, int((x - w * margin) * W)); top = max(0, int((y - h * margin) * H))
    right = min(W, int((x + w + w * margin) * W)); bottom = min(H, int((y + h + h * margin) * H))
    if right - left < 10 or bottom - top < 10:
        return None
    c = img.crop((left, top, right, bottom))
    if c.height < min_h:
        s = min_h / c.height
        c = c.resize((int(c.width * s), min_h), Image.LANCZOS)
    out = io.BytesIO()
    c.save(out, "JPEG", quality=90)
    return out.getvalue()

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
    box = (ev.get("data") or {}).get("box")
    seed_frame = get_frame(eid)
    if not seed_frame:
        return jsonify({"error": "no snapshot for seed event"}), 404
    seed_crop = crop_to_box(seed_frame, box) if box else seed_frame
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
        frame = get_frame(cid)
        if not frame:
            continue
        cbox = (e.get("data") or {}).get("box")
        crop = crop_to_box(frame, cbox) if cbox else frame
        sim = float(torch.nn.functional.cosine_similarity(seed_feat, embed(crop), dim=0))
        thumb = put_thumb(cid, get_frame(cid, boxed=True) or frame)
        cropb = make_crop(frame, cbox) if cbox else None
        crop_thumb = put_thumb(f"{cid}_crop", cropb) if cropb else None
        out.append({
            "event_id": cid,
            "camera": e.get("camera"),
            "start_time": e.get("start_time"),
            "similarity": round(sim, 4),
            "thumb": thumb,
            "crop": crop_thumb,
        })
    out.sort(key=lambda x: -x["similarity"])
    print(f"sightings for {eid}: {len(out)} candidates in {time.time()-t0:.1f}s", flush=True)
    return jsonify({"seed": eid, "window_minutes": window // 60, "candidates": out[:TOP_N]})

if __name__ == "__main__":
    load_model()
    app.run(host="0.0.0.0", port=8090)
