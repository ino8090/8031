#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import time
import os
import re
import json
import requests

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = "premium"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

# M3U ve Logo Bağlantıları
M3U_URL = "https://raw.githubusercontent.com/ino8090/8031/refs/heads/main/Planetp.m3u"
LOGO_URL = "https://raw.githubusercontent.com/ino8090/3152/refs/heads/main/1787225007657.png"

GIST_ID = "34df90330e4b0daeed9a5b516c1c368d"

# YENİ TOKENİNİ AŞAĞIDAKİ ALANA YAZ (Buraya mesaj olarak atma!)
GH_TOKEN = os.getenv("GH_TOKEN", "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")

STREAM_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def get_gist_state():
    """Gist'ten en son kalınan video indeksini ve saniyeyi okur."""
    if not GIST_ID:
        print("⚠️ GIST_ID tanımlı değil!")
        return 0, 0
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"token {GH_TOKEN}"} if GH_TOKEN else {}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            files = res.json().get("files", {})
            if "state.json" in files:
                content = files["state.json"]["content"]
                data = json.loads(content)
                idx = data.get("last_index", 0)
                sec = data.get("last_seconds", 0)
                print(f"✅ Gist başarıyla okundu -> İndeks: {idx}, Saniye: {sec}")
                return idx, sec
        else:
            print(f"❌ Gist okuma başarısız! HTTP Durum Kodu: {res.status_code}")
    except Exception as e:
        print(f"⚠️ Gist okuma hatası: {e}")
    return 0, 0

def update_gist_state(index, seconds):
    """Gist üzerine güncel konumu kaydeder."""
    if not GIST_ID or not GH_TOKEN:
        print("⚠️ GIST_ID veya GH_TOKEN eksik!")
        return
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"token {GH_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        payload = {
            "files": {
                "state.json": {
                    "content": json.dumps({"last_index": int(index), "last_seconds": int(seconds)})
                }
            }
        }
        res = requests.patch(url, headers=headers, json=payload, timeout=5)
        if res.status_code == 200:
            print(f"💾 Konum Gist'e Kaydedildi -> İndeks: {index}, Saniye: {int(seconds)}")
        else:
            print(f"❌ Gist KAYIT HATASI! HTTP Kodu: {res.status_code} (Token geçersiz veya silinmiş)")
    except Exception as e:
        print(f"⚠️ Gist güncelleme hatası: {e}")

def get_m3u_playlist(m3u_url):
    """M3U listesindeki tüm yayın linklerini çekip liste olarak döner."""
    try:
        headers = {'User-Agent': STREAM_USER_AGENT}
        response = requests.get(m3u_url, headers=headers, timeout=15)
        if response.status_code == 200:
            lines = response.text.splitlines()
            playlist = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith('#') and line.startswith('http'):
                    playlist.append(line)
            return playlist
    except Exception as e:
        print(f"⚠️ M3U çekme hatası: {e}")
    return [m3u_url]

def download_logo():
    try:
        headers = {'User-Agent': STREAM_USER_AGENT}
        response = requests.get(LOGO_URL, headers=headers, timeout=15)
        if response.status_code == 200 and len(response.content) > 0:
            with open('logo.png', 'wb') as f:
                f.write(response.content)
            print("✅ Logo başarıyla indirildi.")
    except Exception as e:
        print(f"⚠️ Logo indirme hatası: {e}")

def start_m3u_stream():
    download_logo()
    
    current_index, last_seconds = get_gist_state()

    while True:
        playlist = get_m3u_playlist(M3U_URL)
        if not playlist:
            time.sleep(10)
            continue
            
        if current_index >= len(playlist):
            current_index = 0
            last_seconds = 0

        target_stream_url = playlist[current_index]
        
        print("=" * 60)
        print("📺 SSH101 Canlı M3U Aktarım Yayını (1080p - 1000k) Başlatılıyor")
        print(f"📡 Kaynak Yayın     : {target_stream_url}")
        print(f"⏱️ Başlangıç Saniyesi: {last_seconds}")
        print(f"🚀 Hedef RTMP       : {RTMP_SERVER}")
        print("=" * 60)

        has_logo = os.path.exists('logo.png') and os.path.getsize('logo.png') > 0

        if has_logo:
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[main];'
                '[1:v]scale=-2:80[logo];'
                '[main][logo]overlay=47:42[v]'
            )
            logo_input = ['-i', 'logo.png']
        else:
            filter_str = (
                '[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,'
                'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[v]'
            )
            logo_input = []

        headers_arg = f"User-Agent: {STREAM_USER_AGENT}\r\n"

        command = [
            'ffmpeg',
            '-headers', headers_arg,
            '-ss', str(last_seconds),
            '-re',
            '-i', target_stream_url
        ] + logo_input + [
            '-filter_complex', filter_str,
            '-map', '[v]',
            '-map', '0:a?',
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-pix_fmt', 'yuv420p',
            '-b:v', '1000k',
            '-maxrate', '1200k',
            '-bufsize', '2400k',
            '-g', '50',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-f', 'flv',
            RTMP_SERVER
        ]

        print("▶ FFmpeg başlatıldı, 1000k yayın iletiliyor...")
        
        process = subprocess.Popen(
            command,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        last_save_time = time.time()
        current_stream_seconds = last_seconds

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break
            
            if "time=" in line:
                time_match = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
                if time_match:
                    hrs, mins, secs = time_match.groups()
                    played_seconds = int(hrs) * 3600 + int(mins) * 60 + float(secs)
                    current_stream_seconds = last_seconds + played_seconds
                    
                    # Her 15 saniyede bir son konumu Gist'e kaydet
                    if time.time() - last_save_time > 15:
                        update_gist_state(current_index, current_stream_seconds)
                        last_save_time = time.time()

        # Yayın kapandığında veya kesildiğinde saniyeyi koru
        last_seconds = current_stream_seconds
        update_gist_state(current_index, last_seconds)

        print(f"⚠️ Yayın durdu! Kaldığı yer -> İndeks: {current_index}, Saniye: {int(last_seconds)}")
        print("5 saniye sonra tekrar bağlanılıyor...")
        time.sleep(5)

if __name__ == "__main__":
    start_m3u_stream()
