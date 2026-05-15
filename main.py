"""飞书 Claude 智能体 - FastAPI 后端"""
import json, os, logging, hashlib, base64
from fastapi import FastAPI, Request
import httpx
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 环境变量检查（启动时检查，缺啥一目了然）
REQUIRED_ENV = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "CLAUDE_API_KEY"]
missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
if missing:
    logger.error("缺少环境变量: %s", ", ".join(missing))
    logger.error("请在 Render 的 Environment Variables 中设置后重新部署")

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

anthropic = Anthropic(api_key=CLAUDE_API_KEY) if CLAUDE_API_KEY else None

app = FastAPI()

# 飞书 token 缓存
token_cache = {"token": "", "expires": 0}

async def get_tenant_token():
    import time
    if token_cache["token"] and time.time() < token_cache["expires"]:
        return token_cache["token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        )
        data = resp.json()
        token_cache["token"] = data["tenant_access_token"]
        token_cache["expires"] = time.time() + data.get("expire", 7000)
        return token_cache["token"]


async def send_message(open_id: str, text: str):
    token = await get_tenant_token()
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )


def decrypt(timestamp: str, nonce: str, body: dict) -> dict:
    encrypt = body.get("encrypt")
    if not encrypt:
        return body
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = hashlib.sha256(FEISHU_ENCRYPT_KEY.encode()).digest()[:32]
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16))
    decryptor = cipher.decryptor()
    raw = decryptor.update(base64.b64decode(encrypt)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads(unpadder.update(raw) + unpadder.finalize())


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logger.info("收到飞书回调: %s", json.dumps(body, ensure_ascii=False)[:200])

    ts = request.headers.get("X-Lark-Request-Timestamp", "0")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")

    event_body = decrypt(ts, nonce, body)
    challenge = event_body.get("challenge")
    if challenge:
        return {"challenge": challenge}

    event = event_body.get("event", {})
    if event.get("event_type") != "im.message.receive_v1":
        return {"ok": True}

    message = event.get("message", {})
    chat_type = message.get("chat_type", "")
    msg_type = message.get("message_type", "")

    if chat_type == "p2p" and msg_type == "text":
        sender_id = event["sender"]["sender_id"]["open_id"]
        content = json.loads(message["content"]).get("text", "")

        try:
            msg = await anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                system="你是飞书上的 Claude 智能助手，用中文回答用户问题。",
                messages=[{"role": "user", "content": content}],
            )
            reply = msg.content[0].text
        except Exception as e:
            reply = f"抱歉，我出错了：{e}"

        await send_message(sender_id, reply)

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok", "env_ok": bool(CLAUDE_API_KEY)}
