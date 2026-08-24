#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH101 M3U -> RTMP Aktarım Yayıncısı
======================================
Özellikler:
- GitHub Gist üzerinde kalınan konumu (indeks + saniye) saklar
- GitHub API rate limit'ini header'lardan takip eder ve adaptif davranır
- Aynı anda birden fazla instance çalışmasını lock file ile engeller
- Konsola ve log dosyasına (stream.log) yapılandırılmış log basar
- Ağ/API hatalarında üstel geri çekilme (exponential backoff) uygular
"""

import subprocess
import sys
import time
import os
import re
import json
import logging
import fcntl
import atexit
from datetime import datetime, timezone

import requests

# ===================== AYARLAR =====================
RTMP_URL = "rtmp://ssh101.bozztv.com:1935/ssh101"
STREAM_KEY = "premium"
RTMP_SERVER = f"{RTMP_URL}/{STREAM_KEY}"

M3U_URL = "https://raw.githubusercontent.com/ino8090/8031/refs/heads/main/Planetp.m3u"
LOGO_URL = "https://raw.githubusercontent.com/ino8090/3152/refs/heads/main/1787225007657.png"

GIST_ID = "34df90330e4b0daeed9a5b516c1c368d"

# Token SADECE ortam değişkeninden okunur. Koda asla yazma.
#   export GH_TOKEN="ghp_..."
GH_TOKEN = os.getenv("GH_TOKEN")

STREAM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Gist'e ne sıklıkla konum kaydedilsin (saniye). Rate limit'e takılırsa
# script bunu otomatik olarak geçici şekilde artırır.
BASE_SAVE_INTERVAL = int(os.getenv("SAVE_INTERVAL", "15"))
MAX_SAVE_INTERVAL = 300  # rate limit'te en fazla 5 dakikaya kadar geri çekilir

LOCK_FILE_PATH = "/tmp/stream_m3u.lock"
LOG_FILE_PATH = "stream.log"

# ===================== LOGLAMA =====================
logger = logging.getLogger("stream_m3u")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

try:
    _file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
except Exception as e:
    logger.warning(f"Log dosyası açılamadı ({LOG_FILE_PATH}): {e}")


# ===================== TEKİL INSTANCE KİLİDİ =====================
class SingleInstanceLock:
    """Aynı anda script'in birden fazla kopyasının çalışmasını engeller.
    Çoklu instance, rate limit tüketiminin en sık nedenlerinden biridir."""

    def __init__(self, lock_path: str):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.lock_path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except (IOError, OSError):
            self._fh.close()
            self._fh = None
            return False

    def release(self):
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
            try:
                os.remove(self.lock_path)
            except FileNotFoundError:
                pass


# ===================== GITHUB API SARMALAYICI =====================
class GitHubGistClient:
    """GitHub Gist API'sine erişimi rate-limit farkındalığıyla yönetir."""

    def __init__(self, token: str, gist_id: str):
        self.token = token
        self.gist_id = gist_id
        self.session = requests.Session()
        self.rate_limit_remaining = None
        self.rate_limit_reset = None
        self._rate_limited_until = 0.0

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _update_rate_limit_from_headers(self, headers: dict):
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
        if reset is not None:
            self.rate_limit_reset = int(reset)

        if self.rate_limit_remaining is not None and self.rate_limit_remaining <= 1:
            # Limit tükenmiş; reset zamanına kadar bekle
            self._rate_limited_until = self.rate_limit_reset or (time.time() + 60)
            reset_dt = datetime.fromtimestamp(self._rate_limited_until, tz=timezone.utc)
            logger.warning(
                f"GitHub API rate limit tükendi. Sıfırlanma zamanı: "
                f"{reset_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limited_until

    def seconds_until_reset(self) -> float:
        return max(0.0, self._rate_limited_until - time.time())

    def check_token(self) -> bool:
        """Token'ın geçerliliğini doğrular, hesap adını loglar."""
        if not self.token:
            logger.warning(
                "GH_TOKEN tanımlı değil! Gist okuma/yazma devre dışı. "
                "Ayarlamak için: export GH_TOKEN=\"ghp_...\""
            )
            return False
        try:
            res = self.session.get(
                "https://api.github.com/user",
                headers=self._headers(),
                timeout=10,
            )
            self._update_rate_limit_from_headers(res.headers)
            if res.status_code == 200:
                user = res.json().get("login", "bilinmiyor")
                logger.info(f"GH_TOKEN geçerli. Kullanıcı: {user}")
                return True
            elif res.status_code == 401:
                logger.error(
                    "GH_TOKEN geçersiz veya iptal edilmiş (401). "
                    "Yeni bir classic Personal Access Token oluştur ve 'gist' scope'unu işaretle."
                )
                return False
            else:
                logger.warning(f"Token kontrolü beklenmedik yanıt: {res.status_code} - {res.text[:200]}")
                return False
        except requests.RequestException as e:
            logger.warning(f"Token kontrolü sırasında ağ hatası: {e}")
            return False

    def get_state(self) -> tuple[int, float]:
        """Gist'ten son kalınan indeks ve saniyeyi okur."""
        if not self.gist_id or not self.token:
            return 0, 0.0
        if self.is_rate_limited():
            logger.info(f"Rate limit aktif, Gist okunmadan sıfırdan başlanıyor "
                        f"(sıfırlanmaya {int(self.seconds_until_reset())}sn var).")
            return 0, 0.0
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            res = self.session.get(url, headers=self._headers(), timeout=10)
            self._update_rate_limit_from_headers(res.headers)

            if res.status_code == 200:
                files = res.json().get("files", {})
                if "state.json" in files:
                    data = json.loads(files["state.json"]["content"])
                    idx = data.get("last_index", 0)
                    sec = data.get("last_seconds", 0)
                    logger.info(f"Gist okundu -> İndeks: {idx}, Saniye: {sec}")
                    return idx, sec
                logger.warning("Gist içinde state.json bulunamadı, sıfırdan başlanıyor.")
            elif res.status_code == 401:
                logger.error("Gist okuma hatası: token geçersiz/iptal edilmiş (401).")
            elif res.status_code == 403:
                self._handle_403(res)
            elif res.status_code == 404:
                logger.error("Gist okuma hatası: GIST_ID bulunamadı (404).")
            else:
                logger.error(f"Gist okuma başarısız: {res.status_code} - {res.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Gist okuma sırasında ağ hatası: {e}")
        return 0, 0.0

    def update_state(self, index: int, seconds: float) -> bool:
        """Gist'e güncel konumu kaydeder. Başarılıysa True döner."""
        if not self.gist_id or not self.token:
            return False
        if self.is_rate_limited():
            logger.debug("Rate limit aktif, kayıt atlandı.")
            return False
        try:
            url = f"https://api.github.com/gists/{self.gist_id}"
            payload = {
                "files": {
                    "state.json": {
                        "content": json.dumps({
                            "last_index": int(index),
                            "last_seconds": int(seconds),
                        })
                    }
                }
            }
            res = self.session.patch(url, headers=self._headers(), json=payload, timeout=10)
            self._update_rate_limit_from_headers(res.headers)

            if res.status_code == 200:
                logger.info(f"Konum kaydedildi -> İndeks: {index}, Saniye: {int(seconds)}")
                return True
            elif res.status_code == 401:
                logger.error("Gist kayıt hatası: token geçersiz/iptal edilmiş (401).")
            elif res.status_code == 403:
                self._handle_403(res)
            elif res.status_code == 404:
                logger.error("Gist kayıt hatası: GIST_ID hatalı ya da erişim yok (404).")
            else:
                logger.error(f"Gist kayıt hatası: {res.status_code} - {res.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Gist kayıt sırasında ağ hatası: {e}")
        return False

    def _handle_403(self, res: requests.Response):
        body = res.text
        if "rate limit" in body.lower():
            logger.warning(
                "GitHub API rate limit'e takıldı (403). "
                f"Kalan istek: {self.rate_limit_remaining}. Detay: {body[:200]}"
            )
        else:
            logger.error(
                f"403 Forbidden (rate limit değil — muhtemelen token'da 'gist' scope'u eksik). "
                f"Detay: {body[:200]}"
            )


# ===================== YARDIMCI FONKSİYONLAR =====================
def get_m3u_playlist(m3u_url: str) -> list[str]:
    try:
        headers = {"User-Agent": STREAM_USER_AGENT}
        response = requests.get(m3u_url, headers=headers, timeout=15)
        if response.status_code == 200:
            playlist = [
                line.strip() for line in response.text.splitlines()
                if line.strip() and not line.startswith("#") and line.startswith("http")
            ]
            if playlist:
                return playlist
            logger.warning("M3U listesi boş döndü.")
        else:
            logger.warning(f"M3U listesi alınamadı! HTTP: {response.status_code}")
    except requests.RequestException as e:
        logger.warning(f"M3U çekme hatası: {e}")
    return [m3u_url]


def download_logo() -> bool:
    try:
        headers = {"User-Agent": STREAM_USER_AGENT}
        response = requests.get(LOGO_URL, headers=headers, timeout=15)
        if response.status_code == 200 and len(response.content) > 0:
            with open("logo.png", "wb") as f:
                f.write(response.content)
            logger.info("Logo başarıyla indirildi.")
            return True
        logger.warning(f"Logo indirilemedi! HTTP: {response.status_code}")
    except requests.RequestException as e:
        logger.warning(f"Logo indirme hatası: {e}")
    return False


def build_ffmpeg_command(target_stream_url: str, last_seconds: float, has_logo: bool) -> list[str]:
    if has_logo:
        filter_str = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[main];"
            "[1:v]scale=-2:80[logo];"
            "[main][logo]overlay=47:42[v]"
        )
        logo_input = ["-i", "logo.png"]
    else:
        filter_str = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black[v]"
        )
        logo_input = []

    headers_arg = f"User-Agent: {STREAM_USER_AGENT}\r\n"

    return (
        [
            "ffmpeg",
            "-headers", headers_arg,
            "-ss", str(last_seconds),
            "-re",
            "-i", target_stream_url,
        ]
        + logo_input
        + [
            "-filter_complex", filter_str,
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-b:v", "1000k",
            "-maxrate", "1200k",
            "-bufsize", "2400k",
            "-g", "50",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-f", "flv",
            RTMP_SERVER,
        ]
    )


# ===================== ANA DÖNGÜ =====================
def start_m3u_stream():
    gist_client = GitHubGistClient(GH_TOKEN, GIST_ID)
    gist_client.check_token()

    download_logo()

    current_index, last_seconds = gist_client.get_state()

    while True:
        playlist = get_m3u_playlist(M3U_URL)
        if not playlist:
            logger.warning("Playlist alınamadı, 10sn sonra tekrar denenecek.")
            time.sleep(10)
            continue

        if current_index >= len(playlist):
            current_index = 0
            last_seconds = 0

        target_stream_url = playlist[current_index]

        logger.info("=" * 60)
        logger.info("SSH101 Canlı M3U Aktarım Yayını (1080p - 1000k) Başlatılıyor")
        logger.info(f"Kaynak Yayın      : {target_stream_url}")
        logger.info(f"Başlangıç Saniyesi: {last_seconds}")
        logger.info(f"Hedef RTMP        : {RTMP_SERVER}")
        logger.info("=" * 60)

        has_logo = os.path.exists("logo.png") and os.path.getsize("logo.png") > 0
        command = build_ffmpeg_command(target_stream_url, last_seconds, has_logo)

        logger.info("FFmpeg başlatıldı, 1000k yayın iletiliyor...")

        try:
            process = subprocess.Popen(
                command,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
        except FileNotFoundError:
            logger.critical("ffmpeg bulunamadı! Kurulu olduğundan emin ol (apt install ffmpeg).")
            sys.exit(1)

        # Rate limit'e takıldığımızda kayıt aralığını adaptif olarak büyütürüz;
        # limit normale döndüğünde tekrar BASE_SAVE_INTERVAL'a iniyoruz.
        save_interval = BASE_SAVE_INTERVAL
        last_save_time = time.time()
        current_stream_seconds = last_seconds

        while True:
            line = process.stderr.readline()
            if not line and process.poll() is not None:
                break

            if "time=" in line:
                time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                if time_match:
                    hrs, mins, secs = time_match.groups()
                    played_seconds = int(hrs) * 3600 + int(mins) * 60 + float(secs)
                    current_stream_seconds = last_seconds + played_seconds

                    if time.time() - last_save_time > save_interval:
                        if gist_client.is_rate_limited():
                            # Rate limit sürerken kaydı erteleyip aralığı genişlet
                            save_interval = min(save_interval * 2, MAX_SAVE_INTERVAL)
                        else:
                            success = gist_client.update_state(current_index, current_stream_seconds)
                            save_interval = BASE_SAVE_INTERVAL if success else min(
                                save_interval * 2, MAX_SAVE_INTERVAL
                            )
                        last_save_time = time.time()

        last_seconds = current_stream_seconds
        gist_client.update_state(current_index, last_seconds)

        logger.warning(f"Yayın durdu! Kaldığı yer -> İndeks: {current_index}, Saniye: {int(last_seconds)}")
        logger.info("5 saniye sonra tekrar bağlanılıyor...")
        time.sleep(5)


if __name__ == "__main__":
    lock = SingleInstanceLock(LOCK_FILE_PATH)
    if not lock.acquire():
        logger.critical(
            f"Script zaten çalışıyor! ({LOCK_FILE_PATH} kilitli). "
            "Çoklu instance rate limit'i hızla tüketir, çıkılıyor."
        )
        sys.exit(1)

    atexit.register(lock.release)

    try:
        start_m3u_stream()
    except KeyboardInterrupt:
        logger.info("Kullanıcı tarafından durduruldu (Ctrl+C).")
    except Exception as e:
        logger.critical(f"Beklenmeyen hata: {e}", exc_info=True)
        sys.exit(1)
