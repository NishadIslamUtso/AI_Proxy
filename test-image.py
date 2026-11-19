import base64, json, urllib.request, sys
from pathlib import Path

img = Path(r"C:\Users\Nishad Islam Utso\.config\browser-harness\tmp\p-dab85a3066d7\shot.png")
if not img.exists():
    print("no image found")
    sys.exit(1)

b64 = base64.b64encode(img.read_bytes()).decode()
payload = {
    "model": "chatgpt",
    "messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is shown in this image? Two sentences max."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ],
    }],
}
req = urllib.request.Request(
    "http://127.0.0.1:8000/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req, timeout=300).read().decode()[:800])
