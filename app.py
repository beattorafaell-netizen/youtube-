#!/usr/bin/env python3
"""
YouTube Upload Scheduler - Web Interface
=========================================
Servidor web local para agendar uploads no YouTube.

Instalar dependências:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib flask

Rodar:
    python app.py
    Acesse: http://localhost:5000
"""

import os
import time
import pickle
import logging
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ─────────────────────────────────────────────
#  CONFIGURAÇÕES
# ─────────────────────────────────────────────

PASTA_UPLOADS  = Path("videos")
PASTA_ENVIADOS = Path("enviados")
CREDENTIALS_FILE = "client_secrets.json"
TOKEN_FILE = "token.pickle"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Fila de vídeos agendados: lista de dicts
fila_agendados = []
fila_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=".")

# ─────────────────────────────────────────────
#  AUTENTICAÇÃO
# ─────────────────────────────────────────────

def autenticar():
    credenciais = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as f:
            credenciais = pickle.load(f)
    if not credenciais or not credenciais.valid:
        if credenciais and credenciais.expired and credenciais.refresh_token:
            credenciais.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            credenciais = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(credenciais, f)
    return build("youtube", "v3", credentials=credenciais)

# ─────────────────────────────────────────────
#  UPLOAD
# ─────────────────────────────────────────────

def fazer_upload(youtube, caminho_video: Path, titulo: str, descricao: str, privacidade: str) -> dict:
    corpo = {
        "snippet": {
            "title": titulo,
            "description": descricao,
            "categoryId": "22",
        },
        "status": {"privacyStatus": privacidade},
    }
    media = MediaFileUpload(str(caminho_video), mimetype="video/*", resumable=True, chunksize=10 * 1024 * 1024)
    try:
        req = youtube.videos().insert(part="snippet,status", body=corpo, media_body=media)
        resposta = None
        while resposta is None:
            _, resposta = req.next_chunk()
        video_id = resposta.get("id", "")
        destino = PASTA_ENVIADOS / caminho_video.name
        if destino.exists():
            destino = PASTA_ENVIADOS / f"{caminho_video.stem}__{int(time.time())}{caminho_video.suffix}"
        caminho_video.rename(destino)
        return {"ok": True, "video_id": video_id, "url": f"https://youtu.be/{video_id}"}
    except HttpError as e:
        return {"ok": False, "erro": str(e)}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

# ─────────────────────────────────────────────
#  WORKER — verifica fila a cada 30s
# ─────────────────────────────────────────────

def worker():
    log.info("Worker de agendamento iniciado.")
    youtube = autenticar()
    while True:
        agora = datetime.now()
        with fila_lock:
            pendentes = [v for v in fila_agendados if v["status"] == "aguardando"]
            for item in pendentes:
                horario = datetime.fromisoformat(item["horario"])
                if agora >= horario:
                    item["status"] = "enviando"
                    log.info("Enviando '%s'...", item["titulo"])
                    resultado = fazer_upload(
                        youtube,
                        Path(item["caminho"]),
                        item["titulo"],
                        item["descricao"],
                        item["privacidade"],
                    )
                    if resultado["ok"]:
                        item["status"] = "enviado"
                        item["url"] = resultado["url"]
                        log.info("✅ Enviado: %s", resultado["url"])
                    else:
                        item["status"] = "erro"
                        item["erro"] = resultado["erro"]
                        log.error("❌ Erro: %s", resultado["erro"])
        time.sleep(30)

# ─────────────────────────────────────────────
#  ROTAS
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/agendar", methods=["POST"])
def agendar():
    if "video" not in request.files:
        return jsonify({"ok": False, "erro": "Nenhum vídeo enviado"}), 400

    arquivo = request.files["video"]
    titulo = request.form.get("titulo", arquivo.filename.rsplit(".", 1)[0])
    descricao = request.form.get("descricao", "Vídeo enviado automaticamente.")
    horario = request.form.get("horario", datetime.now().isoformat())
    privacidade = request.form.get("privacidade", "public")

    PASTA_UPLOADS.mkdir(exist_ok=True)
    PASTA_ENVIADOS.mkdir(exist_ok=True)

    caminho = PASTA_UPLOADS / arquivo.filename
    arquivo.save(str(caminho))

    item = {
        "id": int(time.time() * 1000),
        "titulo": titulo,
        "descricao": descricao,
        "horario": horario,
        "privacidade": privacidade,
        "caminho": str(caminho),
        "arquivo": arquivo.filename,
        "status": "aguardando",
        "url": None,
        "erro": None,
    }

    with fila_lock:
        fila_agendados.append(item)

    log.info("Agendado: '%s' para %s", titulo, horario)
    return jsonify({"ok": True, "item": item})

@app.route("/fila")
def listar_fila():
    with fila_lock:
        return jsonify(fila_agendados)

@app.route("/cancelar/<int:item_id>", methods=["DELETE"])
def cancelar(item_id):
    with fila_lock:
        for item in fila_agendados:
            if item["id"] == item_id and item["status"] == "aguardando":
                item["status"] = "cancelado"
                caminho = Path(item["caminho"])
                if caminho.exists():
                    caminho.unlink()
                return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "Item não encontrado ou já processado"}), 404

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    PASTA_UPLOADS.mkdir(exist_ok=True)
    PASTA_ENVIADOS.mkdir(exist_ok=True)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    log.info("=" * 50)
    log.info("  Acesse: http://localhost:5000")
    log.info("=" * 50)
    app.run(debug=False, port=5000)
