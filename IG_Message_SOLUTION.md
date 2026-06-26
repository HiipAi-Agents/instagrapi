# Giải pháp gửi & nhận tin nhắn Instagram qua API (instagrapi)

## Vấn đề

Instagram không cung cấp public API cho DM. Để gửi tin nhắn tự động cần giả lập Android app qua **Instagram Private API**. Các rào cản chính:

| Phương thức | Kết quả | Lý do |
|---|---|---|
| Instagram Graph API (official) | ❌ Chỉ dùng cho Business | Không hỗ trợ DM cá nhân |
| Scrape web Instagram | ❌ Bị block nhanh | Rate limit + bot detection |
| `direct_send` → tài khoản đã follow | ✅ OK | Không bị giới hạn |
| `direct_send` → tài khoản chưa follow | ✅ OK (vào Message Requests) | Target cần accept trước khi reply |
| `direct_send` → tài khoản private + chưa follow | ⚠️ Vào Requests | Target phải chấp nhận request |

## Phân loại target & cách xử lý

```
Target là tài khoản Business / Creator (public)
  └─→ direct_send trực tiếp ✅ (tin vào inbox chính)

Target là tài khoản cá nhân public chưa follow mình
  └─→ direct_send ✅ (tin vào Message Requests của họ)

Target là tài khoản private chưa follow mình
  └─→ direct_send ✅ (tin vào Message Requests — target phải accept)
```

**Không cần group trick như Facebook** — Instagram cho phép gửi DM tới bất kỳ user_id nào, tin sẽ nằm ở Message Requests nếu chưa có quan hệ follow.

---

## Authentication — Login bằng Cookie từ Browser

Instagram session được lưu trong cookie `sessionid`. Lấy bằng cách export cookies từ browser:

### Cách lấy cookie file

1. Vào `instagram.com`, đăng nhập tài khoản cần dùng
2. Cài extension **Cookie-Editor** (Chrome/Firefox)
3. Click icon extension → **Export** → **Export as JSON**
4. Lưu file, ví dụ: `cookies_ig.json`

Format file:
```json
[
  { "name": "sessionid", "value": "8195986836%3AKBG68ot1...", "domain": ".instagram.com", ... },
  { "name": "csrftoken", "value": "g7jIWkmY...", ... },
  { "name": "ds_user_id", "value": "8195986836", ... }
]
```

Cookie quan trọng nhất là `sessionid` — bắt đầu bằng numeric user_id.

---

## Cách lấy user_id từ username

Instagram yêu cầu **numeric user_id** để gửi DM, không dùng username trực tiếp được:

### RapidAPI (nhanh, không cần session IG)

Dùng endpoint `social-api4.p.rapidapi.com/v1/info`:

```python
import requests

resp = requests.get(
    "https://social-api4.p.rapidapi.com/v1/info",
    params={"username_or_id_or_url": "mrbeast"},
    headers={
        "x-rapidapi-key": "YOUR_RAPIDAPI_KEY",
        "x-rapidapi-host": "social-api4.p.rapidapi.com",
    }
)
user_id = resp.json()["data"]["id"]  # "2278169415"
```



---

## Gửi tin nhắn

### Gửi cho user mới (lần đầu)

**Phương thức:** `POST /direct_v2/threads/broadcast/text/`

**Cách hoạt động:**

Khi truyền `user_ids` thay vì `thread_ids`, Instagram server sẽ **tự động tạo thread mới** nếu chưa tồn tại giữa 2 người, hoặc reuse thread cũ nếu đã từng nhắn. Client không cần biết `thread_id` trước — server trả về `thread_id` trong response.

```
Client (Android giả lập)
  │
  │  POST /api/v1/direct_v2/threads/broadcast/text/
  │  Body: recipient_users=[[target_user_id]]
  │        text="..."
  │        mutation_token=<random_token>     ← idempotency key
  │        client_context=<same_token>
  │        action=send_item
  │
  ▼
Instagram Server
  ├─ Tìm thread hiện có giữa sender ↔ target
  ├─ Nếu chưa có → tạo thread mới
  └─ Trả về payload gồm: item_id, thread_id, timestamp, ...
```

**Vị trí tin nhắn ở phía target:**

| Quan hệ | Tin nằm ở |
|---|---|
| Target đã follow sender | Inbox chính (Primary) |
| Target chưa follow sender | Message Requests — target phải Accept |
| Target là Business/Creator | Inbox chính hoặc General tùy setting |

**Lưu ý về `mutation_token`:** Cùng token gửi 2 lần → Instagram chỉ xử lý 1 lần (tránh duplicate). Token được gen random mỗi lần gọi.

**Text có URL** → method tự động chuyển sang `broadcast/link/` thay vì `broadcast/text/`.

```python
from instagrapi import Client

client = Client(rapidapi_key="YOUR_RAPIDAPI_KEY")
client.login_from_cookie_file("cookies_ig.json")

user_id = client.user_id_from_username("target_username")
msg = client.direct_send("Xin chào! Đây là tin nhắn từ Hiip 👋", user_ids=[user_id])

# msg trả về DirectMessage object
print("thread_id:", msg.thread_id)   # dùng để reply sau này
print("message_id:", msg.id)
print("timestamp:", msg.timestamp)
```

### Reply vào thread có sẵn

**Phương thức:** `POST /direct_v2/threads/broadcast/text/`  — giống hệt, chỉ đổi param.

Khi truyền `thread_ids` thay vì `user_ids`, Instagram gửi thẳng vào thread đó mà không cần tìm/tạo thread.

```python
client.direct_send("Cảm ơn bạn đã phản hồi!", thread_ids=[msg.thread_id])
```

`thread_id` lấy từ:
- `msg.thread_id` sau lần gửi đầu tiên
- `data["message"]["thread_id"]` từ event `message` khi lắng nghe (xem phần bên dưới)

---

## Code hoàn chỉnh — Gửi & Reply

```python
from instagrapi import Client

client = Client(rapidapi_key="YOUR_RAPIDAPI_KEY")
client.login_from_cookie_file("cookies_ig.json")
print("Logged in as:", client.username)

# Gửi cho user mới
user_id = client.user_id_from_username("uthuuyentran1810")
msg = client.direct_send("Xin chào từ Hiip! 👋", user_ids=[user_id])
print("Sent, thread_id:", msg.thread_id)

# Reply vào thread vừa tạo
client.direct_send("Đây là tin nhắn tiếp theo.", thread_ids=[msg.thread_id])
```


---

## Lắng nghe tin nhắn đến

Dùng `RealtimeClient` — kết nối MQTT đến `edge-mqtt.facebook.com:443`, subscribe các topic DM của Instagram.

### Các events

| Event | Ý nghĩa |
|---|---|
| `message` | **Tin nhắn mới** trong DM thread |
| `thread_update` | Thread bị update (tên, thành viên...) |
| `typing` | Ai đó đang gõ |
| `seen` | Tin nhắn đã được đọc |
| `presence` | User online/offline |
| `receive` | Tất cả raw MQTT packets |

### Code listen

```python
from instagrapi import Client

client = Client()
client.login_from_cookie_file("cookies_ig.json")

def on_message(data):
    msg = data.get("message", {})
    thread_id = msg.get("thread_id")
    text = msg.get("text")
    sender = msg.get("user_id")
    print(f"[Thread {thread_id}] {sender}: {text}")

    # Auto-reply ví dụ
    if text and "hello" in text.lower():
        client.direct_send("Xin chào! Cảm ơn bạn đã liên hệ Hiip 👋", thread_ids=[thread_id])

def on_typing(data):
    print("Typing:", data.get("thread_id"), data.get("value"))

def on_seen(data):
    print("Seen:", data.get("thread_id"))

# Đăng ký handlers
client.realtime_on("message", on_message)
client.realtime_on("typing", on_typing)
client.realtime_on("seen", on_seen)

# Kết nối và subscribe inbox
rt = client.realtime_connect()
rt.direct_subscribe()   # sync seq_id từ inbox hiện tại

print(f"Listening as @{client.username}...")
while True:
    client.realtime_read_once()
```

### Kiến trúc Realtime

```
RealtimeClient
  └─ SocketMQTToTTransport  (TCP/TLS → edge-mqtt.facebook.com:443)
      └─ MQTToT Protocol    (MQTT variant của Meta)
          └─ Thrift encoding + zlib compress
              └─ Topics: 88, 133, 134, 135, 146, 149, 150
                  └─ Topic 146 (MESSAGE_SYNC) → DM events
```

---

## Giới hạn & lưu ý

- **Cookie hết hạn** — `sessionid` thường có hạn ~1 năm, khi hết cần export lại
- **Rate limit** — Gửi quá nhiều DM nhanh có thể bị Instagram tạm block tài khoản
- **Message Requests** — Tin nhắn đến người lạ sẽ nằm ở Requests, không hiển thị ngay ở inbox chính của target
- **Realtime loop là blocking** — Nếu dùng trong production, chạy trong `threading.Thread` riêng
- **`direct_subscribe()`** cần gọi sau `realtime_connect()` để sync `seq_id` từ inbox, tránh bỏ sót tin nhắn cũ
- Không cần account là bạn bè với target để gửi DM — chỉ cần `user_id` hợp lệ
