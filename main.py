"""飞书中转站 - 把消息转给 CC 处理"""
import json, os, logging, hashlib, base64
from fastapi import FastAPI, Request
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")

app = FastAPI()

# 待处理消息队列: [{"from": "ou_xxx", "msg": "你好", "time": "...", "msg_id": "om_xxx"}, ...]
pending_messages = []
# 已处理消息 ID 集合（去重）
processed_ids = set()

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

    raw = base64.b64decode(encrypt)
    key = hashlib.sha256(FEISHU_ENCRYPT_KEY.encode()).digest()
    iv = raw[:16]
    data = raw[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad_len = plain[-1]
    return json.loads(plain[:-pad_len])


@app.post("/")
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logger.info("收到飞书回调")

    ts = request.headers.get("X-Lark-Request-Timestamp", "0")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")

    try:
        event_body = decrypt(ts, nonce, body)
    except Exception as e:
        logger.error("解密失败: %s", e)
        return {"error": "decrypt failed"}

    challenge = event_body.get("challenge")
    if challenge:
        return {"challenge": challenge}

    header = event_body.get("header", {})
    et = header.get("event_type", "") or event_body.get("event", {}).get("event_type", "")
    if et != "im.message.receive_v1":
        return {"ok": True}

    event = event_body.get("event", {})
    message = event.get("message", {})
    chat_type = message.get("chat_type", "")
    msg_type = message.get("message_type", "")
    msg_id = message.get("message_id", "")

    if chat_type == "p2p" and msg_type == "text":
        sender_id = event["sender"]["sender_id"]["open_id"]
        content = json.loads(message["content"]).get("text", "")

        # 去重，加入待处理队列
        if msg_id not in processed_ids:
            processed_ids.add(msg_id)
            pending_messages.append({
                "from": sender_id,
                "msg": content,
                "time": message.get("create_time", ""),
                "msg_id": msg_id,
            })
            logger.info("收到消息: %s -> %s", sender_id, content)

        # 先回复一个正在处理的提示
        await send_message(sender_id, "✅ 消息已收到，正在处理请稍候...")

    return {"ok": True}


# CC 用来获取消息的接口
@app.get("/api/inbox")
async def inbox():
    msgs = list(pending_messages)
    return {"count": len(msgs), "messages": msgs}


# CC 回复后标记已处理
@app.post("/api/done")
async def mark_done(data: dict):
    msg_id = data.get("msg_id", "")
    global pending_messages
    pending_messages = [m for m in pending_messages if m["msg_id"] != msg_id]
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
