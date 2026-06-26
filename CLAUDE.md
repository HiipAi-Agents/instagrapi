# Project Context

## Repo
Fork của `subzeroid/instagrapi` — Python client cho Instagram Private API (giả lập Android app).

## Thay đổi đã thêm vào repo này

### 1. `instagrapi/__init__.py`
- Thêm param `rapidapi_key: Optional[str] = None` vào `Client.__init__`
- Set `self.rapidapi_key = rapidapi_key`

### 2. `instagrapi/mixins/user.py`
- Thêm `import requests` ở đầu file
- Thêm `RAPIDAPI_HOST = "social-api4.p.rapidapi.com"` module-level
- Thêm class var `rapidapi_key: Optional[str] = None`
- Thêm method `user_id_from_username_rapidapi(username)` — gọi `social-api4.p.rapidapi.com/v1/info`, trả về `data["id"]`
- Sửa `user_id_from_username()`: nếu có `self.rapidapi_key` thì gọi RapidAPI thay vì IG

### 3. `instagrapi/mixins/auth.py`
- Thêm method `login_from_cookie_file(path)` — đọc JSON array (format Cookie-Editor export), extract `sessionid`, gọi `login_by_sessionid()`

## File tài liệu
- `IG_Message_SOLUTION.md` — giải thích đầy đủ cách gửi/nhận IG DM

## RapidAPI key
Set via `Client(rapidapi_key="YOUR_KEY")`. Key không được commit — lưu trong env var hoặc file riêng.

## Threads DM Library
Đã tách thành repo riêng: `/Users/vudinh/Documents/work/hiip/threadsapi`
- Package: `threadsapi`
- API: GraphQL tại `www.threads.com/api/graphql` (web, không cần app/emulator)
- Auth: session cookie từ threads.com browser login
