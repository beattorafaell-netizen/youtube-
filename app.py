#!/usr/bin/env python3
"""
YouTube Upload Scheduler - Web Interface
=========================================
Com notificação no Telegram e histórico de uploads.

Instalar dependências:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib flask requests

Rodar:
    python app.py
    Acesse: http://localhost:5000
"""

import os
import time
import json
import pickle
import shutil
import logging
import threading
import requests
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
HISTORICO_FILE   = "historico.json"

# Telegram
TELEGRAM_TOKEN   = "8608511631:AAFG9qPvifnJziLCL4UE-rMwMZWH37d27pk"
TELEGRAM_CHAT_ID = "8144548560"

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
#  HISTÓRICO
# ─────────────────────────────────────────────

def carregar_historico() -> list:
    if Path(HISTORICO_FILE).exists():
        with open(HISTORICO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def salvar_historico(historico: list):
    with open(HISTORICO_FILE, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)

def adicionar_historico(titulo: str, url: str, privacidade: str):
    historico = carregar_historico()
    historico.insert(0, {
        "titulo":     titulo,
        "url":        url,
        "privacidade": privacidade,
        "data":       datetime.now().isoformat(),
    })
    salvar_historico(historico)

# ─────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────

def enviar_telegram(mensagem: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensagem,
            "parse_mode": "HTML",
        }, timeout=10)
        log.info("📱 Telegram notificado.")
    except Exception as e:
        log.warning("⚠️ Falha ao notificar Telegram: %s", e)

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
        str(caminho_video), mimetype="video/*",
        resumable=True, chunksize=10 * 1024 * 1024,
    )

    try:
        req = youtube.videos().insert(part="snippet,status", body=corpo, media_body=media)
        resposta = None
        while resposta is None:
            _, resposta = req.next_chunk()

        video_id = resposta.get("id", "")
        url = f"https://youtu.be/{video_id}"
        log.info("✅ Upload concluído: %s", url)

        # Thumbnail
        thumb_path = item.get("thumb_caminho")
        if thumb_path and Path(thumb_path).exists():
            try:
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(thumb_path, mimetype="image/*")
                ).execute()
                log.info("🖼️ Thumbnail aplicada.")
            except HttpError as e:
                log.warning("⚠️ Thumbnail falhou: %s", e)

        # Move vídeo para /enviados (se não for recorrente)
        if not item.get("recorrente"):
            destino = PASTA_ENVIADOS / caminho_video.name
            if destino.exists():
                destino = PASTA_ENVIADOS / f"{caminho_video.stem}__{int(time.time())}{caminho_video.suffix}"
            shutil.move(str(caminho_video), str(destino))

        # Salva no histórico
        adicionar_historico(item["titulo"], url, item["privacidade"])

        # Notifica Telegram
        priv_emoji = {"public": "🌍", "unlisted": "🔗", "private": "🔒"}.get(item["privacidade"], "")
        enviar_telegram(
            f"✅ <b>Vídeo postado no YouTube!</b>\n\n"
            f"🎬 <b>{item['titulo']}</b>\n"
            f"{priv_emoji} {item['privacidade'].capitalize()}\n"
            f"🔗 {url}"
        )

        return {"ok": True, "video_id": video_id, "url": url}

    except HttpError as e:
        enviar_telegram(f"❌ <b>Falha no upload!</b>\n\n🎬 {item['titulo']}\n⚠️ {e}")
        return {"ok": False, "erro": str(e)}
    except Exception as e:
        return {"ok": False, "erro": str(e)}

# ─────────────────────────────────────────────
#  PRÓXIMO HORÁRIO RECORRENTE
# ─────────────────────────────────────────────

def proximo_horario(item: dict) -> str:
    base = datetime.fromisoformat(item["horario"])
    agora = datetime.now()
    delta = timedelta(weeks=1) if item.get("recorrencia") == "semanal" else timedelta(days=1)
    while base <= agora:
        base += delta
    return base.isoformat()

# ─────────────────────────────────────────────
#  WORKER
# ─────────────────────────────────────────────

def worker():
    log.info("Worker iniciado.")
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
                resultado = fazer_upload(youtube, item)

                if resultado["ok"]:
                    item["url"] = resultado["url"]
                    item["ultimo_envio"] = datetime.now().isoformat()
                    if item.get("recorrente"):
                        item["url_historico"].append(resultado["url"])
                        item["horario"] = proximo_horario(item)
                        item["status"] = "aguardando"
                    else:
                        item["status"] = "enviado"
                else:
                    item["status"] = "erro"
                    item["erro"] = resultado["erro"]

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

    for p in [PASTA_UPLOADS, PASTA_ENVIADOS, PASTA_THUMBS]:
        p.mkdir(exist_ok=True)

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

    return jsonify({"ok": True, "item": item})

@app.route("/fila")
def listar_fila():
    with fila_lock:
        return jsonify(fila_agendados)

@app.route("/historico")
def listar_historico():
    return jsonify(carregar_historico())

@app.route("/cancelar/<int:item_id>", methods=["DELETE"])
def cancelar(item_id):
    with fila_lock:
        for item in fila_agendados:
            if item["id"] == item_id and item["status"] == "aguardando":
                item["status"] = "cancelado"
                Path(item["caminho"]).unlink(missing_ok=True)
                return jsonify({"ok": True})
    return jsonify({"ok": False, "erro": "Item não encontrado"}), 404

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    for p in [PASTA_UPLOADS, PASTA_ENVIADOS, PASTA_THUMBS]:
        p.mkdir(exist_ok=True)

    threading.Thread(target=worker, daemon=True).start()

    log.info("=" * 50)
    log.info("  Acesse: http://localhost:5000")
    log.info("=" * 50)
    app.run(debug=False, port=5000)
