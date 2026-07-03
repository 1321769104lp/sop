import base64
import hashlib
import hmac
import time

import requests


def generate_sign(secret: str, timestamp: int) -> str:
    """生成飞书自定义机器人签名。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def send_feishu_message(webhook_url: str, secret: str, text: str) -> dict:
    """发送文本消息到飞书群机器人。Secret 为空时按未签名机器人发送。"""
    if not webhook_url:
        raise ValueError("请先配置 FEISHU_WEBHOOK_URL")

    payload = {
        "msg_type": "text",
        "content": {
            "text": text
        }
    }

    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = str(timestamp)
        payload["sign"] = generate_sign(secret, timestamp)

    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()
