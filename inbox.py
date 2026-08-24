#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os
import json
import requests
import signal

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = "inbox1"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

CHANNEL_NAME = "kanal1"
STATE_FILE_NAME = f"state_{CHANNEL_NAME}.json"

M3U_FILE = "pars.m3u"
LOGO_FILE = "1787069925822.png"

GIST_ID = "34df90330e4b0daeed9a5b516c1c368d"
GH_TOKEN = os.getenv("GH_TOKEN", "")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"

# Sinyal işleyicisi ve ana döngünün paylaştığı durum
_state = {
    "process": None,
    "current_index": 0,
    "current_seconds": 0,
    "shutting_down": False,
}


def get_gist_state():
    if not GIST_ID:
        return 0, 0
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            files = res.json().get("files", {})
            if STATE_FILE_NAME in files:
                content = files[STATE_FILE_NAME]["content"]
                data = json.loads(content)
                idx = int(data.get("last_index", 0))
                sec = int(data.get("last_seconds", 0))
                print(f"✅ GIST OKUNDU -> Film Index: {idx}, Saniye: {sec}", flush=True)
                return idx, sec
        else:
            print(f"❌ Gist okuma HTTP {res.status_code}", flush=True)
    except Exception as e:
        print(f"⚠️ Gist okuma hatası: {e}", flush=True)
    return 0, 0


def update_gist_state(index, seconds, retries=2):
    """Gist'e yazar. Başarısız olursa kısa bir bekleme ile tekrar dener."""
    if not GIST_ID or not GH_TOKEN:
        print("⚠️ GIST_ID veya GH_TOKEN eksik, kaydedilemedi!", flush=True)
        return False
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    payload = {
        "files": {
            STATE_FILE_NAME: {
                "content": json.dumps({"last_index": int(index), "last_seconds": int(seconds)})
            }
        }
    }
    for attempt in range(1, retries + 2):
        try:
            res = requests.patch(url, headers=headers, json=payload, timeout=6)
            if res.status_code == 200:
                print(f"💾 GIST YAZILDI -> Index: {index}, Saniye: {int(seconds)} (deneme {attempt})", flush=True)
                return True
            print(f"⚠️ Gist yazma HTTP {res.status_code} (deneme {attempt})", flush=True)
        except Exception as e:
            print(f"⚠️ Gist yazma hatası: {e} (deneme {attempt})", flush=True)
        time.sleep(1)
    print("❌ Gist yazma tüm denemelerde başarısız oldu!", flush=True)
    return False


def _kill_ffmpeg():
    proc = _state["process"]
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass


def handle_shutdown(signum, frame):
    if _state["shutting_down"]:
        os._exit(1)
    _state["shutting_down"] = True

    idx = _state["current_index"]
    sec = _state["current_seconds"]
    print(f"\n🛑 Kapanış sinyali alındı (signal={signum}). Son durum yazılıyor: Index {idx}, Saniye {sec}", flush=True)

    _kill_ffmpeg()
    update_gist_state(idx, sec)

    print("✅ Kapanış tamamlandı, çıkılıyor.", flush=True)
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def get_m3u_playlist(m3u_file_path):
    try:
        if os.path.exists(m3u_file_path):
            with open(m3u_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
            playlist = []
            current_title = "Film Yayini"
            for line in lines:
                line = line.strip()
                if line.startswith('#EXTINF:'):
                    if ',' in line:
                        current_title = line.split(',', 1)[1].strip()
                elif line and not line.startswith('#') and line.startswith('http'):
                    playlist.append((line, current_title))
                    current_title = "Film Yayini"
            return playlist
    except Exception as e:
        print(f"⚠️ M3U okuma hatası: {e}", flush=True)
    return []


def check_logo():
    return os.path.exists(LOGO_FILE) and os.path.getsize(LOGO_FILE) > 0


def escape_ffmpeg_text(text):
    return text.replace(":", "\\:").replace("'", "").replace("%", "\\%")


def start_m3u_stream():
    has_logo = check_logo()

    current_index, last_seconds = get_gist_state()
    _state["current_index"] = current_index
    _state["current_seconds"] = last_seconds

    while True:
        playlist = get_m3u_playlist(M3U_FILE)
        if not playlist:
            time.sleep(10)
            continue

        if current_index >= len(playlist):
            current_index = 0
            last_seconds = 0

        _state["current_index"] = current_index
        _state["current_seconds"] = last_seconds

        target_stream_url, film_title = playlist[current_index]
        clean_title = escape_ffmpeg_text(film_title)

        print("=" * 60, flush=True)
        print(f"🎬 Oynatılan Film : {film_title}", flush=True)
        print(f"⏱️ Başlangıç Saniyesi: {last_seconds}", flush=True)
        print("=" * 60, flush=True)

        text_filter = (
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{clean_title}':fontsize=0:fontcolor=white:"
            f"x=w-tw-45:y=h-th-45"
        )

        if has_logo:
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[main];'
                '[1:v]scale=-2:80[logo];'
                '[main][logo]overlay=50:50[v_logo];'
                f'[v_logo]{text_filter}[v]'
            )
            logo_input = ['-i', LOGO_FILE]
        else:
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[v_base];'
                f'[v_base]{text_filter}[v]'
            )
            logo_input = []

        headers_arg = f"User-Agent: {STREAM_USER_AGENT}\r\n"

        command = ['ffmpeg', '-headers', headers_arg]
        if last_seconds > 0:
            command.extend(['-ss', str(last_seconds)])

        command.extend(['-re', '-i', target_stream_url])
        command.extend(logo_input)
        command.extend([
            '-filter_complex', filter_str,
            '-map', '[v]',
            '-map', '0:a?',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-pix_fmt', 'yuv420p',
            '-b:v', '2000k',
            '-maxrate', '2000k',
            '-bufsize', '4000k',
            '-g', '50',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-f', 'flv',
            RTMP_SERVER
        ])

        # start_new_session=True: sinyal önce Python'a gelsin, ffmpeg'i kendimiz
        # düzgünce kapatalım (aksi halde ikisi aynı anda öldürülüp ffmpeg'in
        # muxer'ı yarım kalabilir ve/veya kapanış kodu hiç çalışmayabilir).
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        _state["process"] = process

        start_time = time.time()
        start_offset = last_seconds
        last_save_time = time.time()

        while process.poll() is None:
            time.sleep(1)
            elapsed = int(time.time() - start_time)
            _state["current_seconds"] = start_offset + elapsed

            # Kayıt aralığı kısaltıldı: kayıp penceresi en fazla ~5 sn
            if time.time() - last_save_time >= 5:
                update_gist_state(current_index, _state["current_seconds"])
                last_save_time = time.time()

        if process.returncode == 0:
            current_index += 1
            last_seconds = 0
            update_gist_state(current_index, 0)
        else:
            last_seconds = _state["current_seconds"]
            update_gist_state(current_index, last_seconds)

        _state["current_index"] = current_index
        _state["current_seconds"] = last_seconds

        time.sleep(3)


if __name__ == "__main__":
    start_m3u_stream()
