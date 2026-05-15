"""飞书 DeepSeek 智能体 - FastAPI 后端"""
import json, os, logging, hashlib, base64
from fastapi import FastAPI, Request
import httpx
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

missing = [v for v in ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "DEEPSEEK_API_KEY"] if not os.getenv(v)]
if missing:
    logger.error("缺少环境变量: %s", ", ".join(missing))

client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    timeout=httpx.Timeout(60.0, connect=10.0),
) if DEEPSEEK_API_KEY else None

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

    raw = base64.b64decode(encrypt)
    key = hashlib.sha256(FEISHU_ENCRYPT_KEY.encode()).digest()
    iv = raw[:16]          # IV 在密文的前 16 字节
    data = raw[16:]         # 剩下的才是真正的密文

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    # PKCS7 去填充
    pad_len = plain[-1]
    return json.loads(plain[:-pad_len])


# 同时处理 / 和 /webhook 两个路径
@app.post("/")
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    logger.info("收到飞书回调: %s", json.dumps(body, ensure_ascii=False)[:200])

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

    logger.info("解密成功, event_body keys: %s, type: %s",
                 list(event_body.keys()), event_body.get("type"))

    event = event_body.get("event", {})
    header = event_body.get("header", {})
    et = header.get("event_type", "") or event.get("event_type", "")
    logger.info("事件类型: %s, event keys: %s", et, list(event.keys()))

    if et != "im.message.receive_v1":
        return {"ok": True}

    message = event.get("message", {})
    chat_type = message.get("chat_type", "")
    msg_type = message.get("message_type", "")
    logger.info("消息: chat_type=%s msg_type=%s", chat_type, msg_type)

    if chat_type == "p2p" and msg_type == "text":
        sender_id = event["sender"]["sender_id"]["open_id"]
        content = json.loads(message["content"]).get("text", "")
        logger.info("来自 %s: %s", sender_id, content)

        try:
            logger.info("调用 DeepSeek API: %s", content[:100])
            resp = await client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": "你是飞书上的 DeepSeek 智能助手，用中文回答用户问题。"},
                    {"role": "user", "content": content},
                ],
            )
            reply = resp.choices[0].message.content or ""
            logger.info("DeepSeek 回复成功: %s", reply[:100])
        except Exception as e:
            reply = f"抱歉，我出错了：{e}"
            logger.error("DeepSeek 调用失败: %s", e)

        try:
            await send_message(sender_id, reply)
            logger.info("消息已发送到飞书")
        except Exception as e:
            logger.error("飞书消息发送失败: %s", e)

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok", "env_ok": bool(DEEPSEEK_API_KEY)}
