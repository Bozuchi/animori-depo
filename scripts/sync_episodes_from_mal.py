"""
scripts/sync_episodes_from_mal.py — MyAnimeList Resmi Bölüm Sayılarını Eşleme Scripti

MyAnimeList'ten günlük taranan resmi açık kaynak veri setini (anime-offline-database) kullanarak:
1. Her animenin MyAnimeList resmi toplam bölüm sayısını eşleştirir.
2. api/anime/{mal_id}.json dosyalarındaki jikan.episodes alanını günceller.
3. Bölüm sayısı bulunamayan veya bilinmeyen animeler için null girer.
4. api/animes.json vitrin indeksini ve api/version.json dosyasını yeniden inşa eder.
"""

import os
import sys
import json
import re
import urllib.request
import time

# Proje kök dizinini Python path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage_manager import StorageManager
from src.logger import setup_logger

logger = setup_logger("SyncEpisodes")

OFFLINE_DB_URL = "https://github.com/cedya77/anime-offline-database/releases/download/latest/anime-offline-database.jsonl"


def fetch_mal_episodes_mapping() -> dict[int, int]:
    """
    anime-offline-database üzerinden MAL ID -> Resmi Bölüm Sayısı haritası çıkarır.
    """
    logger.info("📥 MyAnimeList kaynaklı veri seti indiriliyor/taranıyor...")
    req = urllib.request.Request(
        OFFLINE_DB_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )

    mal_to_episodes = {}
    t0 = time.time()
    count = 0

    with urllib.request.urlopen(req, timeout=60) as resp:
        for line in resp:
            count += 1
            try:
                data = json.loads(line.decode("utf-8"))
            except Exception:
                continue

            episodes = data.get("episodes")
            sources = data.get("sources") or []

            for s in sources:
                if "myanimelist.net/anime/" in s:
                    match = re.search(r"myanimelist\.net/anime/(\d+)", s)
                    if match:
                        mid = int(match.group(1))
                        # Sadece geçerli pozitif bir sayı varsa haritaya al
                        if episodes and int(episodes) > 0:
                            mal_to_episodes[mid] = int(episodes)

            if count % 10000 == 0:
                logger.info(f"⏳ {count} veri satırı işlendi... ({time.time() - t0:.1f}sn)")

    logger.info(
        f"✅ MAL veri seti tarandı: Toplam {len(mal_to_episodes)} anime için bölüm sayısı hazırlandı ({time.time() - t0:.1f}sn)."
    )
    return mal_to_episodes


def run():
    storage = StorageManager()
    mal_to_episodes = fetch_mal_episodes_mapping()

    anime_dir = storage.anime_dir
    if not os.path.exists(anime_dir):
        logger.error(f"Anime dizini bulunamadı: {anime_dir}")
        return

    json_files = [f for f in sorted(os.listdir(anime_dir)) if f.endswith(".json")]
    logger.info(f"📂 {len(json_files)} yerel anime dosyası taranıyor...")

    updated_count = 0
    already_correct = 0
    unknown_count = 0

    for i, fname in enumerate(json_files):
        name_no_ext = fname[:-5]
        if not name_no_ext.isdigit():
            continue

        mal_id = int(name_no_ext)
        file_path = os.path.join(anime_dir, fname)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({fname}): {e}")
            continue

        jikan = data.get("jikan")
        if jikan is None:
            jikan = {}
            data["jikan"] = jikan

        # MAL haritasından bölüm sayısını al (yoksa None = bilinmiyor)
        target_episodes = mal_to_episodes.get(mal_id) or None
        current_episodes = jikan.get("episodes")

        if target_episodes is None:
            unknown_count += 1

        # Değer farklıysa güncelle (mevcut 0 değerleri de null'a normalize edilir)
        if current_episodes != target_episodes:
            jikan["episodes"] = target_episodes
            data["jikan"] = jikan

            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                updated_count += 1
            except Exception as e:
                logger.error(f"Dosya yazılamadı ({fname}): {e}")
        else:
            already_correct += 1

        if (i + 1) % 1500 == 0:
            logger.info(f"⏳ İlerleme: {i + 1}/{len(json_files)} dosya kontrol edildi...")

    logger.info("==========================================")
    logger.info(f"🎉 Bölüm senkronizasyonu tamamlandı!")
    logger.info(f"📝 Güncellenen dosya sayısı: {updated_count}")
    logger.info(f"✔️ Zaten doğru olan dosya sayısı: {already_correct}")
    logger.info(f"❓ Bölüm sayısı bilinmeyen: {unknown_count}")
    logger.info("==========================================")

    # Vitrini ve versiyonları güncelle
    logger.info("🔨 Vitrin indeksi (api/animes.json) yeniden oluşturuluyor...")
    total_indexed = storage.build_index()
    storage.update_versions()
    logger.info(f"✅ animes.json ve version.json başarıyla güncellendi! ({total_indexed} anime)")


if __name__ == "__main__":
    run()
