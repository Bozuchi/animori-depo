"""
src/fribb_mapper.py — Fribb/anime-lists Eşleme ve Delta Takip Motoru

TMDB, MyAnimeList (MAL) ve AniList arasındaki kimlik eşlemelerini yerel önbellek
ve HTTP Conditional GET (ETag / If-None-Match) üzerinden yönetir.
Uzaktaki dataset güncellendiğinde delta (değişen/eklenen) animeleri tespit eder.

Veri Kaynağı: https://github.com/Fribb/anime-lists
"""

import os
import json
import time
import hashlib
import requests
from typing import Optional

try:
    from src.logger import setup_logger
except ImportError:
    from logger import setup_logger

logger = setup_logger("FribbMapper")

CACHE_DIR = "data"
CACHE_FILE = os.path.join(CACHE_DIR, "anime-list-mini.json")
STATE_FILE = os.path.join(CACHE_DIR, "fribb_state.json")
FRIBB_RAW_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-mini.json"


class FribbMapper:
    """Fribb datasetini yöneten, delta güncellemeleri algılayan ve TMDB eşlemesi sağlayan sınıf."""

    def __init__(
        self,
        cache_file: str = CACHE_FILE,
        state_file: str = STATE_FILE,
        auto_load: bool = True,
    ):
        self.cache_file = cache_file
        self.state_file = state_file

        self.mal_to_tmdb: dict[int, dict] = {}
        self.tmdb_season_to_mal: dict[tuple[int, int], int] = {}
        self.movie_tmdb_to_mal: dict[int, int] = {}

        self.loaded = False
        self.state = self._load_state()

        if auto_load:
            self.load()

    def _load_state(self) -> dict:
        """data/fribb_state.json dosyasını yükler."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"fribb_state.json okunamadı: {e}")
        return {"etag": None, "last_modified": None, "sha256": None, "last_checked": None}

    def _save_state(self):
        """fribb_state.json dosyasını kaydeder."""
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"fribb_state.json kaydedilemedi: {e}")

    def check_and_update_remote(self) -> tuple[bool, set[int]]:
        """
        Fribb GitHub deposunu HTTP Conditional GET (ETag) ile kontrol eder.
        
        Returns:
            (is_updated, diff_mal_ids):
                - is_updated: Dataset güncellendi mi?
                - diff_mal_ids: Değişen veya yeni eklenen MAL ID'ler kümesi.
        """
        os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)
        headers = {
            "User-Agent": "Animori-Depo/2.0",
        }
        if self.state.get("etag"):
            headers["If-None-Match"] = self.state["etag"]

        logger.info("📡 Fribb dataset güncelleme kontrolü yapılıyor (GitHub)...")
        try:
            resp = requests.get(FRIBB_RAW_URL, headers=headers, timeout=25)
        except Exception as e:
            logger.warning(f"Fribb sunucusuna erişilemedi: {e}. Mevcut yerel önbellek kullanılacak.")
            return False, set()

        # 304: Değişiklik yok
        if resp.status_code == 304:
            logger.info("✅ Fribb dataset güncel (304 Not Modified).")
            self.state["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save_state()
            return False, set()

        if resp.status_code != 200:
            logger.warning(f"Fribb HTTP {resp.status_code} döndürdü. Önbellek kullanılacak.")
            return False, set()

        # 200: Yeni veri geldi
        new_content = resp.content
        new_sha256 = hashlib.sha256(new_content).hexdigest()

        if self.state.get("sha256") == new_sha256 and os.path.exists(self.cache_file):
            logger.info("✅ Fribb içerik hash'i aynı, güncelleme gerekmiyor.")
            self.state["etag"] = resp.headers.get("ETag") or self.state.get("etag")
            self.state["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._save_state()
            return False, set()

        logger.info(f"🔄 Fribb datasetinde yeni sürüm tespit edildi ({len(new_content):,} bayt).")

        # Önceki veriyi oku (fark hesabı için)
        old_mapping = dict(self.mal_to_tmdb)

        # Yeni veriyi diske yaz
        try:
            with open(self.cache_file, "wb") as f:
                f.write(new_content)
        except Exception as e:
            logger.error(f"Fribb önbellek dosyası yazılamadı: {e}")
            return False, set()

        # State'i güncelle
        self.state["etag"] = resp.headers.get("ETag")
        self.state["last_modified"] = resp.headers.get("Last-Modified")
        self.state["sha256"] = new_sha256
        self.state["last_checked"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save_state()

        # İndeksleri baştan oluştur
        self.load(force_reload=True)

        # Farkı hesapla (hangi MAL ID'ler eklendi veya değişti?)
        diff_mal_ids = set()
        for mal_id, new_info in self.mal_to_tmdb.items():
            old_info = old_mapping.get(mal_id)
            if old_info != new_info:
                diff_mal_ids.add(mal_id)

        logger.info(f"📊 Fribb güncellemesi tamamlandı: {len(diff_mal_ids)} anime etkilendi/güncellendi.")
        return True, diff_mal_ids

    def load(self, force_reload: bool = False):
        """Yerel önbellekten Fribb verisini okur ve bellek içi indeksleri oluşturur."""
        if self.loaded and not force_reload:
            return

        if not os.path.exists(self.cache_file):
            logger.info("Fribb önbellek dosyası yerelde bulunamadı, indiriliyor...")
            self.check_and_update_remote()
            if not os.path.exists(self.cache_file):
                logger.error("Fribb dosyası indirilemedi.")
                return

        t0 = time.time()
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                raw_list = json.load(f)
        except Exception as e:
            logger.error(f"Fribb JSON dosyası okunamadı: {e}")
            return

        self.mal_to_tmdb.clear()
        self.tmdb_season_to_mal.clear()
        self.movie_tmdb_to_mal.clear()

        # Öncelik sıralaması:
        # 1. Canonical TV sezonları (tmdb == tvdb)
        # 2. TV / MOVIE tipleri
        # 3. Düşük MAL ID (ana yapım önceliği)
        def sort_key(item):
            season_info = item.get("season", {})
            is_canonical = (
                isinstance(season_info, dict)
                and season_info.get("tmdb") == season_info.get("tvdb")
                and season_info.get("tmdb", 0) > 0
            )
            entry_type = item.get("type", "")
            type_score = 1 if entry_type == "TV" else (2 if entry_type == "MOVIE" else 3)
            canonical_score = 0 if is_canonical else 1
            mal_id = item.get("mal_id") or 9999999
            return (canonical_score, type_score, mal_id)

        sorted_list = sorted(raw_list, key=sort_key)

        for item in sorted_list:
            mal_id = item.get("mal_id")
            if not mal_id:
                continue

            tmdb_info = item.get("themoviedb_id")
            if not tmdb_info or not isinstance(tmdb_info, dict):
                continue

            season_info = item.get("season", {})

            offset_info = item.get("episode_offset")
            offset_val = 0
            if isinstance(offset_info, dict):
                offset_val = offset_info.get("tmdb")
                if offset_val is None:
                    offset_val = offset_info.get("tvdb", 0)
            elif isinstance(offset_info, int):
                offset_val = offset_info
            offset_val = offset_val or 0

            # 1. TV Dizisi eşlemesi
            if "tv" in tmdb_info and tmdb_info["tv"]:
                tmdb_id = tmdb_info["tv"]
                season_num = season_info.get("tmdb", 1) if isinstance(season_info, dict) else 1
                key = (tmdb_id, season_num)

                if key not in self.tmdb_season_to_mal:
                    self.tmdb_season_to_mal[key] = mal_id

                if mal_id not in self.mal_to_tmdb:
                    self.mal_to_tmdb[mal_id] = {
                        "id": tmdb_id,
                        "type": "tv",
                        "season": season_num,
                        "episode_offset": offset_val,
                    }

            # 2. Movie eşlemesi
            elif "movie" in tmdb_info and tmdb_info["movie"]:
                movie_ids = tmdb_info["movie"]
                if isinstance(movie_ids, int):
                    movie_ids = [movie_ids]

                for movie_id in movie_ids:
                    if movie_id not in self.movie_tmdb_to_mal:
                        self.movie_tmdb_to_mal[movie_id] = mal_id

                    if mal_id not in self.mal_to_tmdb:
                        self.mal_to_tmdb[mal_id] = {
                            "id": movie_id,
                            "type": "movie",
                            "season": 1,
                            "episode_offset": offset_val,
                        }

        self.loaded = True
        elapsed = time.time() - t0
        logger.info(
            f"✅ Fribb indekslendi ({elapsed:.2f} sn): {len(self.mal_to_tmdb)} MAL eşlemesi yüklendi."
        )

    def get_tmdb_info(self, mal_id: int | str) -> dict | None:
        """
        MAL ID'den standart TMDB eşleşmesini döner.
        
        Returns:
            {"id": int, "type": "tv"|"movie", "season": int} veya None
        """
        if not self.loaded:
            self.load()

        try:
            m_id = int(mal_id)
        except (ValueError, TypeError):
            return None

        return self.mal_to_tmdb.get(m_id)
