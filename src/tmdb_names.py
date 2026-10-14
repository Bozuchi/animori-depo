"""
tmdb_names.py — TMDB Başlık (name) Çözücü

Fribb eşleşmesiyle gelen TMDB kimliklerini (id / type / season) tamamlamak
için TMDB API'den ilgili başlığı (title / name / original_title) çeker.

Özellikler:
  - Anahtar yoksa sessizce devre dışı kalır (API anahtarı olmayan ortamlarda
    akış kırılmaz; tmdb verisi yine de yazılır, sadece "name" gelmez).
  - Dil önceliği: en-US -> tr-TR -> (dilsiz) original_title.
  (Ana dil İngilizcedir; İngilizce başlık yoksa Türkçe, o da yoksa orijinal
  başlık kullanılır. Böylece Japonca orijinal başlıklar son çare olarak kalır.)
  - data/tmdb_title_cache.json önbelleği: tekrar eden id'ler için API'ye gidilmez.
  - 429 (rate limit) ve geçici ağ hataları için otomatik yeniden deneme.
  - Thread-safe: paralel iş parçacıklarından güvenle kullanılabilir.
"""

import os
import sys
import json
import time
import threading
from typing import Optional

import requests
from dotenv import load_dotenv

from src.logger import setup_logger

logger = setup_logger("TMDBNames")

TMDB_BASE_URL = "https://api.themoviedb.org/3"
LANG_PRIORITY = ["en-US", "tr-TR", None]

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CACHE_PATH = os.path.join(ROOT_DIR, "data", "tmdb_title_cache.json")


class TMDBNameResolver:
    """TMDB API'den başlık çekerek tmdb eşleşmesine 'name' alanı ekler."""

    def __init__(
        self,
        cache_path: str = DEFAULT_CACHE_PATH,
        min_interval: float = 0.08,
        enabled: Optional[bool] = None,
    ):
        self.cache_path = cache_path
        self.min_interval = min_interval

        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cache: dict[str, str] = {}
        self._cache_dirty = False

        load_dotenv(os.path.join(ROOT_DIR, ".env"))
        self.api_key = os.getenv("TMDB_API_KEY")
        self.read_token = os.getenv("TMDB_READ_TOKEN")

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Animori-Database/2.0",
        })
        if self.read_token:
            self.session.headers["Authorization"] = f"Bearer {self.read_token}"

        if enabled is None:
            enabled = bool(self.api_key or self.read_token)

        self.enabled = enabled
        if not self.enabled:
            logger.warning("⚠️ TMDB_API_KEY / TMDB_READ_TOKEN bulunamadı — tmdb.name alanı doldurulmayacak.")

        self._load_cache()

    # ─────────────────────────────────────────────────────────────
    # ÖNBELLEK
    # ─────────────────────────────────────────────────────────────

    def _load_cache(self):
        if not os.path.exists(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
            logger.info(f"💾 TMDB başlık önbelleği yüklendi ({len(self._cache)} kayıt).")
        except Exception as e:
            logger.warning(f"TMDB başlık önbelleği okunamadı: {e}")

    def save_cache(self):
        """Önbelleği diske yazar (gerçek API yanıtlarından oluşan veri)."""
        if not self._cache_dirty:
            return
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_path)
            self._cache_dirty = False
            logger.info(f"💾 TMDB başlık önbelleği kaydedildi: {self.cache_path}")
        except Exception as e:
            logger.warning(f"TMDB başlık önbelleği kaydedilemedi: {e}")

    # ─────────────────────────────────────────────────────────────
    # API
    # ─────────────────────────────────────────────────────────────

    def _rate_limit(self):
        with self._lock:
            elapsed = time.time() - self._last_request
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self._last_request = time.time()

    def _request(self, endpoint: str, params: dict, max_retries: int = 3) -> Optional[dict]:
        self._rate_limit()

        query = dict(params)
        if not self.read_token and self.api_key:
            query["api_key"] = self.api_key

        backoff = 1.0
        for attempt in range(max_retries):
            try:
                resp = self.session.get(f"{TMDB_BASE_URL}{endpoint}", params=query, timeout=15)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 3))
                    logger.warning(f"TMDB 429 → {retry_after} sn bekleniyor...")
                    time.sleep(retry_after)
                    continue
                logger.warning(f"TMDB {resp.status_code} hatası: {endpoint}")
                return None
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.warning(f"TMDB isteği başarısız ({endpoint}): {e}")
                    return None
                time.sleep(backoff)
                backoff *= 2
        return None

    def fetch_title(self, tmdb_id, is_movie: bool) -> Optional[str]:
        """Dil önceliğine göre başlığı çeker; hiçbiri yoksa None döner."""
        media_type = "movie" if is_movie else "tv"
        endpoint = f"/{media_type}/{tmdb_id}"

        for lang in LANG_PRIORITY:
            cache_key = f"{media_type}:{tmdb_id}:{lang or 'orig'}"
            with self._lock:
                cached = self._cache.get(cache_key)

            if cached is None:
                params = {"language": lang} if lang else {}
                data = self._request(endpoint, params)
                cached = "" if data is None else (
                    data.get("title") or data.get("name") or data.get("original_title") or ""
                ).strip()
                with self._lock:
                    self._cache[cache_key] = cached
                    self._cache_dirty = True
            else:
                with self._lock:
                    self._cache[cache_key] = cached  # negatif sonucu da sabitle

            if cached:
                return cached

        return None

    # ─────────────────────────────────────────────────────────────
    # DIŞ API
    # ─────────────────────────────────────────────────────────────

    def resolve(self, tmdb_info: Optional[dict]) -> Optional[dict]:
        """
        tmdb eşleşmesini 'name' alanıyla tamamlar.

        Girdi sözlüğü değiştirilmez; yeni bir sözlük döndürülür.
        Anahtar yoksa / zaten isimliyse / eşleşme bulunamazsa girdi aynen döner.
        """
        if not isinstance(tmdb_info, dict) or not tmdb_info.get("id"):
            return tmdb_info
        if tmdb_info.get("name"):
            return tmdb_info
        if not self.enabled:
            return tmdb_info

        title = self.fetch_title(tmdb_info["id"], tmdb_info.get("type") == "movie")
        if not title:
            return tmdb_info

        enriched = dict(tmdb_info)
        enriched["name"] = title
        return enriched

    def resolve_many(self, tmdb_infos: list[Optional[dict]]) -> list[Optional[dict]]:
        return [self.resolve(info) for info in tmdb_infos]
