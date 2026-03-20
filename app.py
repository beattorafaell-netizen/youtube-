#!/usr/bin/env python3
"""
YouTube Upload Scheduler - Web Interface
=========================================
Servidor web local para agendar uploads no YouTube.
Suporte a thumbnail personalizada e uploads recorrentes.

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
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, send_from_directory
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ─────────────────────────────────────────────
#  CONFIGURAÇÕES
# ─────────────────────────────────────────────

PASTA_UPLOADS    = Path("videos")
PASTA_ENVIADOS   = Path("enviados")
PASTA_THUMBS     = Path("thumbs")
CREDENTIALS_FILE = "client_secrets.json"
TOKEN_FILE       = "token.pickle"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

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
#  UPLOAD + THUMBNAIL
# ─────────────────────────────────────────────

def fazer_upload(youtube, item: dict) -> dict:
    caminho_video = Path(item["caminho"])
    if not caminho_video.exists():
        return {"ok": False, "erro": f"Arquivo não encontrado: {caminho_video}"}

    corpo = {
        "snippet": {
            "title": item["titulo"],
            "description": item["descricao"],
            "categoryId": "22",
        },
        "status": {"privacyStatus": item["privacidade"]},
    }

    media = MediaFileUpload(
        str(caminho_video),
        mimetype="video/*",
        resumable=True,
        chunksize=10 * 1024 * 1024,
    )

    try:
        req = youtube.videos().insert(part="snippet,status", body=corpo, media_body=media)
        resposta = None
        while resposta is None:
            _, resposta = req.next_chunk()

        video_id = resposta.get("id", "")
        log.info("✅ Upload concluído: https://youtu.be/%s", video_id)

        # Sobe thumbnail se existir
        thumb_path = item.get("thumb_caminho")
        if thumb_path and Path(thumb_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumb_path, mimetype="image/*")
                ).execute()
                log.info("🖼️ Thumbnail aplicada ao vídeo %s", video_id)
            except HttpError as e:
                log.warning("⚠️ Thumbnail falhou (conta pode precisar de verificação): %s", e)

        # Move vídeo para /enviados somente se não for recorrente
        if not item.get("recorrente"):
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
#  PRÓXIMO HORÁRIO RECORRENTE
# ─────────────────────────────────────────────

def proximo_horario(item: dict) -> str:
    base = datetime.fromisoformat(item["horario"])
    agora = datetime.now()
    recorrencia = item.get("recorrencia", "diario")
    delta = timedelta(weeks=1) if recorrencia == "semanal" else timedelta(days=1)
    while base <= agora:
        base += delta
    return base.isoformat()

# ─────────────────────────────────────────────
#  WORKER
# ─────────────────────────────────────────────

def worker():
    log.info("Worker de agendamento iniciado.")
    youtube = autenticar()

    while True:
        agora = datetime.now()
        with fila_lock:
            for item in fila_agendados:
                if item["status"] != "aguardando":
                    continue
                if agora < datetime.fromisoformat(item["horario"]):
                    continue

                item["status"] = "enviando"
                log.info("Enviando '%s'...", item["titulo"])
                resultado = fazer_upload(youtube, item)

                if resultado["ok"]:
                    item["url"] = resultado["url"]
                    item["ultimo_envio"] = datetime.now().isoformat()
                    if item.get("recorrente"):
                        item["url_historico"].append(resultado["url"])
                        item["horario"] = proximo_horario(item)
                        item["status"] = "aguardando"
                        log.info("🔁 Reagendado para %s", item["horario"])
                    else:
                        item["status"] = "enviado"
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

    arquivo     = request.files["video"]
    titulo      = request.form.get("titulo", arquivo.filename.rsplit(".", 1)[0])
    descricao   = request.form.get("descricao", "Vídeo enviado automaticamente.")
    horario     = request.form.get("horario", datetime.now().isoformat())
    privacidade = request.form.get("privacidade", "public")
    recorrente  = request.form.get("recorrente", "false") == "true"
    recorrencia = request.form.get("recorrencia", "diario")

    PASTA_UPLOADS.mkdir(exist_ok=True)
    PASTA_ENVIADOS.mkdir(exist_ok=True)
    PASTA_THUMBS.mkdir(exist_ok=True)

    caminho = PASTA_UPLOADS / arquivo.filename
    arquivo.save(str(caminho))

    thumb_caminho = None
    if "thumbnail" in request.files:
        thumb = request.files["thumbnail"]
        if thumb.filename:
            thumb_caminho = str(PASTA_THUMBS / f"{int(time.time())}_{thumb.filename}")
            thumb.save(thumb_caminho)

    item = {
        "id":            int(time.time() * 1000),
        "titulo":        titulo,
        "descricao":     descricao,
        "horario":       horario,
        "privacidade":   privacidade,
        "recorrente":    recorrente,
        "recorrencia":   recorrencia,
        "caminho":       str(caminho),
        "arquivo":       arquivo.filename,
        "thumb_caminho": thumb_caminho,
        "status":        "aguardando",
        "url":           None,
        "url_historico": [],
        "ultimo_envio":  None,
        "erro":          None,
    }

    with fila_lock:
        fila_agendados.append(item)

    log.info("Agendado: '%s' para %s (recorrente=%s)", titulo, horario, recorrente)
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
                Path(item["caminho"]).unlink(missing_ok=True)
                return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "Item não encontrado ou já processado"}), 404

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    PASTA_UPLOADS.mkdir(exist_ok=True)
    PASTA_ENVIADOS.mkdir(exist_ok=True)
    PASTA_THUMBS.mkdir(exist_ok=True)

    threading.Thread(target=worker, daemon=True).start()

    log.info("=" * 50)
    log.info("  Acesse: http://localhost:5000")
    log.info("=" * 50)
    app.run(debug=False, port=5000)
