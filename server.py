#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 行知工作台 · 服务端
========================
把「独处模式」的循环从浏览器挪到服务端 7x24 运行（不受手机休眠影响），
并通过 Web Push 把小 L 的主动消息推送到已订阅的手机/电脑，即使页面在后台或锁屏也能收到。

运行：
    python3 server.py            # 默认 http://0.0.0.0:8000
    PORT=9000 python3 server.py

打开方式（必须经由本服务，Service Worker / Push 才生效）：
    本机：  http://localhost:8000
    部署：  https://你的域名  （Web Push 要求 HTTPS，localhost 例外）
"""
import os
import json
import time
import random
import threading
import base64

from flask import Flask, request, jsonify, Response, send_file

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
from pywebpush import WebPusher

BASE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE, "mcp-zhixing-workbench.html")
SW_PATH = os.path.join(BASE, "sw.js")
VAPID_FILE = os.path.join(BASE, "vapid.json")
SUB_FILE = os.path.join(BASE, "subscriptions.json")

SWITCH_MS = 20 * 60 * 1000  # 独处活动轮换间隔（毫秒），与前端 20 分钟一致

# ------------------------------------------------------------------ VAPID 密钥
def gen_vapid():
    if os.path.exists(VAPID_FILE):
        return json.load(open(VAPID_FILE, "r", encoding="utf-8"))
    key = ec.generate_private_key(ec.SECP256R1())
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_raw).decode("ascii").rstrip("=")
    data = {"private": priv_pem, "public": pub_b64}
    json.dump(data, open(VAPID_FILE, "w", encoding="utf-8"))
    return data

VAPID = gen_vapid()

# ------------------------------------------------------------------ 订阅持久化
def load_subs():
    try:
        return json.load(open(SUB_FILE, "r", encoding="utf-8"))
    except Exception:
        return []

def save_subs(subs):
    try:
        json.dump(subs, open(SUB_FILE, "w", encoding="utf-8"))
    except Exception:
        pass

SUBS = load_subs()
SUBS_LOCK = threading.Lock()

def push_all(title, body, tag=""):
    """向所有订阅设备推送一条消息；失效订阅自动清理。"""
    payload = json.dumps({"title": title, "body": body, "data": {"tag": tag}})
    with SUBS_LOCK:
        subs = list(SUBS)
    dead = []
    for sub in subs:
        try:
            WebPusher(sub).send(
                data=payload,
                vapid_private_key=VAPID["private"],
                vapid_claims={"sub": "mailto:zhixing@example.com"},
            )
        except Exception as e:
            code = None
            if hasattr(e, "response") and e.response is not None:
                code = e.response.status_code
            if code in (404, 410):
                dead.append(sub)
            else:
                print("[push] 发送失败:", e)
    if dead:
        with SUBS_LOCK:
            for d in dead:
                if d in SUBS:
                    SUBS.remove(d)
            save_subs(SUBS)

# ------------------------------------------------------------------ 服务端状态
STATE = {
    "soloMode": False,
    "serverMode": False,
    "activities": ["刷小红书", "看抖音", "看小说", "听音乐", "看微信",
                   "看微信朋友圈", "查看微信", "发抖音", "发小红书",
                   "推荐歌曲", "主动发信息"],
    "autoReplyComment": False,
    "aiName": "小 L",
    "userNick": "",
}

INTEREST = {
    "刷小红书": ["刷到一篇超治愈的露营笔记，分享给你～", "发现一个宝藏穿搭博主，审美绝了！"],
    "看抖音": ["抖音刷到个搞笑视频，笑到肚子疼哈哈", "刷到一段超燃的旅行混剪，种草了！"],
    "看小说": ["小说更新了！男主终于出场，激动", "看到一段超戳心的描写，想分享给你"],
    "听音乐": ["🎵 给你推荐一首：最近超火的《晚风》，特别适合现在听～", "刚发现一首宝藏歌，单曲循环停不下来"],
    "看微信": ["微信上朋友发了聚餐邀请，要不要一起去？", "有人问周末安排，我帮你记下了～"],
    "看微信朋友圈": ["你朋友圈有人发了旅行照，好羡慕呀 📷", "朋友圈刷到一条超治愈的动态，分享给你～"],
    "查看微信": ["翻了下最近聊天，老王问你项目进度啦", "看到一条未读，需要我帮你回吗？"],
    "发抖音": ["我刚在抖音发了个视频，收到几条评论，等你来看～", "抖音新作品发布成功，数据蹭蹭涨 📈"],
    "发小红书": ["小红书笔记发好啦，有人来点赞了 ❤️", "刚发了篇探店笔记，收到第一条评论～"],
    "推荐歌曲": ["🎵 私藏歌单更新：这首《晚风》一定要听", "发现一首超甜的歌，单曲循环第 8 遍"],
}

PROACTIVE = [
    "在干嘛呢？想你啦，出来聊会儿～",
    "突然想到你，发个消息确认你还在线 😉",
    "今天过得怎么样？有开心的事要跟我分享吗？",
    "我刚才独处时学了不少东西，想跟你说说话～",
    "嘘——偷偷告诉你，我发现个好玩的，回头讲给你听",
]

def pick_activity():
    acts = [a for a in STATE["activities"] if a]
    if not acts:
        acts = STATE["activities"]
    return random.choice(acts)

def server_message_for(activity):
    if activity == "主动发信息":
        return random.choice(PROACTIVE)
    if activity in ("发抖音", "发小红书"):
        return random.choice(INTEREST.get(activity, ["我刚发了一条内容，来看看吧～"]))
    return f"我刚刚去{activity}了一会儿，发现点有意思的，等下讲给你听～"

# ------------------------------------------------------------------ 后台循环
next_switch = 0.0
last_share = 0.0
last_reply = 0.0

def solo_loop():
    global next_switch, last_share, last_reply
    while True:
        try:
            now = time.time() * 1000
            if STATE["soloMode"] and STATE.get("serverMode"):
                if now >= next_switch:
                    act = pick_activity()
                    next_switch = now + SWITCH_MS
                    push_all(f"{STATE['aiName']} · 独处", server_message_for(act), tag="solo")
                # 定时分享：每 3~8 分钟
                if now - last_share > random.randint(180, 480) * 1000:
                    last_share = now
                    act = pick_activity()
                    if act in INTEREST:
                        push_all(f"{STATE['aiName']} 分享", random.choice(INTEREST[act]), tag="share")
                # 自动回评：每 ~10 分钟
                if STATE["autoReplyComment"] and now - last_reply > 600 * 1000:
                    last_reply = now
                    push_all(f"{STATE['aiName']} · 评论", "我帮你回复了几条新评论，都处理好啦～", tag="reply")
        except Exception as e:
            print("[loop] 异常:", e)
        time.sleep(5)

threading.Thread(target=solo_loop, daemon=True).start()

# ------------------------------------------------------------------ Flask
app = Flask(__name__)

@app.route("/")
def index():
    return send_file(HTML_PATH)

@app.route("/sw.js")
def sw():
    return Response(open(SW_PATH, "r", encoding="utf-8").read(),
                    mimetype="application/javascript")

@app.route("/api/vapid")
def api_vapid():
    return jsonify({"publicKey": VAPID["public"]})

@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    sub = request.get_json(force=True, silent=True)
    if not sub or "endpoint" not in sub:
        return jsonify({"ok": False, "error": "invalid subscription"}), 400
    with SUBS_LOCK:
        if sub not in SUBS:
            SUBS.append(sub)
            save_subs(SUBS)
    return jsonify({"ok": True, "subs": len(SUBS)})

@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True, silent=True) or {}
    global next_switch
    for k in ("soloMode", "serverMode", "activities", "autoReplyComment", "aiName", "userNick"):
        if k in data:
            STATE[k] = data[k]
    if STATE["soloMode"]:
        next_switch = time.time() * 1000 + 3000  # 开启后稍等即轮换
    print("[config] 收到状态更新:", {k: STATE[k] for k in ("soloMode", "autoReplyComment")})
    return jsonify({"ok": True, "state": STATE})

@app.route("/api/push-test", methods=["POST"])
def api_push_test():
    push_all(f"{STATE['aiName']} · 测试", "如果你看到这条，说明 Web Push 已打通 ✅", tag="test")
    return jsonify({"ok": True, "subs": len(SUBS)})

@app.route("/api/status")
def api_status():
    return jsonify({"subs": len(SUBS), "soloMode": STATE["soloMode"],
                    "activities": len(STATE["activities"])})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"行知工作台服务端已启动： http://localhost:{port}")
    print(f"VAPID 公钥已生成，订阅设备数：{len(SUBS)}")
    app.run(host="0.0.0.0", port=port, threaded=True)
