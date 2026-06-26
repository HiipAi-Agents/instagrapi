# instagrapi — Hiip Fork

Fork của [subzeroid/instagrapi](https://github.com/subzeroid/instagrapi) — Python client cho Instagram Private API, dùng cho Hiip DM workflow.

## Cài đặt

```bash
pip install git+https://github.com/HiipAi-Agents/instagrapi.git
```

Hoặc local:

```bash
pip install -e /path/to/instagrapi
```

---

## Gửi & Nhận Instagram DM

### 1. Lấy cookies từ browser

1. Vào [instagram.com](https://www.instagram.com) → đăng nhập tài khoản cần dùng
2. Cài extension **Cookie-Editor** ([Chrome](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) / [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/))
3. Click icon extension → **Export** → **Export as JSON**
4. Save file, ví dụ `cookies_ig.json`

Format file:

```json
[
  { "name": "sessionid", "value": "8195986836%3AKBG68ot1...", "domain": ".instagram.com" },
  { "name": "csrftoken", "value": "g7jIWkmY..." },
  { "name": "ds_user_id", "value": "8195986836" }
]
```

Cookie `sessionid` được extract tự động — các cookie khác không bắt buộc.

### 2. Khởi tạo client

```python
from instagrapi import Client

client = Client(rapidapi_key="YOUR_RAPIDAPI_KEY")
client.login_from_cookie_file("cookies_ig.json")

print("Logged in as:", client.username)
```

`rapidapi_key` dùng để resolve username → user_id qua [social-api4.p.rapidapi.com](https://rapidapi.com/social-api4-social-api4-default/api/social-api4) mà không cần gọi Instagram API. Bỏ qua nếu không có.

### 3. Gửi DM

#### Gửi cho user mới (lần đầu)

Instagram tự động tạo thread mới nếu chưa tồn tại, hoặc reuse thread cũ.

```python
user_id = client.user_id_from_username("target_username")

msg = client.direct_send("Xin chào từ Hiip! 👋", user_ids=[user_id])
print("Sent, thread_id:", msg.thread_id)
```

Tin nhắn vào **inbox chính** nếu target đã follow sender, hoặc **Message Requests** nếu chưa có quan hệ follow.

#### Reply vào thread có sẵn

```python
client.direct_send("Đây là tin tiếp theo.", thread_ids=[msg.thread_id])
```

#### Gửi hàng loạt

```python
import time

usernames = ["user1", "user2", "user3"]

for username in usernames:
    try:
        user_id = client.user_id_from_username(username)
        msg = client.direct_send(f"Xin chào @{username}!", user_ids=[user_id])
        print(f"✓ {username}: thread {msg.thread_id}")
    except Exception as e:
        print(f"✗ {username}: {e}")
    time.sleep(2)
```

### 4. Lắng nghe tin nhắn đến (Realtime)

Kết nối MQTT đến `edge-mqtt.facebook.com:443`.

```python
from instagrapi import Client

client = Client()
client.login_from_cookie_file("cookies_ig.json")

def on_message(data):
    msg = data.get("message", {})
    thread_id = msg.get("thread_id")
    text = msg.get("text")
    sender_id = msg.get("user_id")
    print(f"[Thread {thread_id}] {sender_id}: {text}")

    # Auto-reply ví dụ
    if text and "hello" in text.lower():
        client.direct_send("Xin chào! Cảm ơn đã liên hệ Hiip 👋", thread_ids=[thread_id])

def on_typing(data):
    print("Typing:", data.get("thread_id"))

def on_seen(data):
    print("Seen:", data.get("thread_id"))

client.realtime_on("message", on_message)
client.realtime_on("typing", on_typing)
client.realtime_on("seen", on_seen)

rt = client.realtime_connect()
rt.direct_subscribe()   # sync seq_id từ inbox, tránh bỏ sót tin cũ

print(f"Listening as @{client.username} ...")
while True:
    client.realtime_read_once()
```

Events hỗ trợ: `message`, `thread_update`, `typing`, `seen`, `presence`, `receive` (raw).

### 5. Đổi account

Chỉ cần đổi file cookies:

```python
client.login_from_cookie_file("cookies_account_b.json")
print("Now logged in as:", client.username)
```

---

## Giới hạn & lưu ý

| Vấn đề | Chi tiết |
|---|---|
| Cookie hết hạn | `sessionid` thường hạn ~1 năm. Khi hết, export lại từ browser |
| Rate limit | Gửi nhiều DM nhanh → Instagram tạm block tài khoản. Thêm `time.sleep()` giữa các request |
| Message Requests | Tin đến người lạ nằm ở Requests — target phải Accept trước khi reply |
| Realtime là blocking | Trong production, chạy `realtime_read_once()` trong `threading.Thread` riêng |
| `sessionid` từ browser | Đôi khi bị reject bởi private mobile API. Nếu bị `login_required`, dùng `login()` với password một lần rồi `dump_settings()` |

Chi tiết kỹ thuật đầy đủ: [IG_Message_SOLUTION.md](IG_Message_SOLUTION.md)
