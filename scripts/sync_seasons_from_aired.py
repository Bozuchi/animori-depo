"""
scripts/sync_seasons_from_aired.py — Eksik Sezon (Season/Mevsim) Bilgilerini Aired Tarihinden Eşleme Scripti

Özellikle filmler, OVA'lar, ONA'lar ve özel bölümlerde (Special) MAL Premiered (Season)
bilgisi sağlamadığı için 'season' alanı null kalabilmektedir.

Bu script:
1. Tüm api/anime/{mal_id}.json dosyalarını tarar.
2. jikan.season değeri null / boş olan serilerde, jikan.aired.from ayına bakarak mevsimi belirler:
   - 1, 2, 3 -> winter
   - 4, 5, 6 -> spring
   - 7, 8, 9 -> summer
   - 10, 11, 12 -> fall
3. Dosyaları kalıcı olarak günceller (jikan.season = 'winter'|'spring'|'summer'|'fall').
4. api/animes.json vitrin indeksini ve api/version.json dosyasını günceller.
"""

import os
import sys
import json
import re

# Proje kök dizinini ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage_manager import StorageManager
from src.logger import setup_logger

logger = setup_logger("SyncSeasons")


def get_season_from_month(month: int) -> str | None:
    if month in (1, 2, 3):
        return "winter"
    elif month in (4, 5, 6):
        return "spring"
    elif month in (7, 8, 9):
        return "summer"
    elif month in (10, 11, 12):
        return "fall"
    return None


def run():
    storage = StorageManager()
    anime_dir = storage.anime_dir

    if not os.path.exists(anime_dir):
        logger.error(f"Anime dizini bulunamadı: {anime_dir}")
        return

    json_files = [f for f in sorted(os.listdir(anime_dir)) if f.endswith(".json")]
    logger.info(f"📂 {len(json_files)} yerel anime dosyası taranıyor...")

    updated_count = 0
    already_has_season = 0
    still_unknown = 0

    for i, fname in enumerate(json_files):
        file_path = os.path.join(anime_dir, fname)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({fname}): {e}")
            continue

        jikan = data.get("jikan")
        if jikan is None:
            continue

        current_season = jikan.get("season")
        if current_season:
            already_has_season += 1
            continue

        # season null/boş ise aired.from ayı üzerinden mevsimi bul
        aired = jikan.get("aired")
        extracted_season = None
        if isinstance(aired, dict) and aired.get("from"):
            from_str = str(aired["from"]).strip()
            m = re.match(r"^\d{4}-(\d{2})", from_str)
            if m:
                try:
                    month = int(m.group(1))
                    extracted_season = get_season_from_month(month)
                except ValueError:
                    extracted_season = None

        if extracted_season:
            jikan["season"] = extracted_season
            data["jikan"] = jikan

            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                updated_count += 1
            except Exception as e:
                logger.error(f"Dosya yazılamadı ({fname}): {e}")
        else:
            still_unknown += 1

        if (i + 1) % 1500 == 0:
            logger.info(f"⏳ İlerleme: {i + 1}/{len(json_files)} dosya kontrol edildi...")

    logger.info("==========================================")
    logger.info(f"🎉 Sezon (Season) senkronizasyonu tamamlandı!")
    logger.info(f"📝 Aired üzerinden sezonu doldurulan: {updated_count}")
    logger.info(f"✔️ Zaten sezonu mevcut olan: {already_has_season}")
    logger.info(f"❓ Yayın tarihi henüz olmayan/bilinmeyen: {still_unknown}")
    logger.info("==========================================")

    # Vitrini ve versiyonları güncelle
    logger.info("🔨 Vitrin indeksi (api/animes.json) yeniden oluşturuluyor...")
    total_indexed = storage.build_index()
    storage.update_versions()
    logger.info(f"✅ animes.json ve version.json başarıyla güncellendi! ({total_indexed} anime)")


if __name__ == "__main__":
    run()
