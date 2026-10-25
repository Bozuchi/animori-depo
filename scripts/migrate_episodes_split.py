"""
scripts/migrate_episodes_split.py — Birleşik 'episodes' dizisini ikiye ayırır

Eski yapıda api/anime/{mal_id}.json içindeki üst seviye "episodes" dizisi hem
Türkanime (turkanime_title, videos, skip_times) hem de MyAnimeList (title, aired,
filler, recap) verilerini bir arada tutuyordu.

Yeni yapı:
    turkanime.episodes   → Türkanime kaynaklı bölümler (title, videos, skip_times, added_date)
    jikan.episode_list   → doğrudan MAL'den gelen bölüm listesi (title, aired, filler, recap)

Üst seviye "episodes" dizisi kaldırılır. Script idempotenttir: ikinci kez
çalıştırıldığında yeni yapıdaki kayıtları korur, sadece eksikleri tamamlar.

Kullanım:
    python scripts/migrate_episodes_split.py            # uygula
    python scripts/migrate_episodes_split.py --dry-run  # sadece raporla
"""

import os
import sys
import json

# Proje kök dizinini Python path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage_manager import StorageManager
from src.logger import setup_logger

logger = setup_logger("MigrateEpisodes")


def merge_by_number(existing: list[dict], new: list[dict]) -> list[dict]:
    """Numaraya göre birleştirir; mevcut (yeni yapıdaki) kayıtlar önceliklidir."""
    by_num = {}
    for ep in new:
        num = ep.get("episode_number")
        if num is not None:
            by_num[num] = ep
    for ep in existing:
        num = ep.get("episode_number")
        if num is not None:
            by_num[num] = ep
    return [by_num[n] for n in sorted(by_num)]


def run(dry_run: bool = False):
    storage = StorageManager()
    anime_dir = storage.anime_dir

    files = sorted(f for f in os.listdir(anime_dir) if f.endswith(".json"))
    logger.info(f"📂 {len(files)} anime dosyası taranıyor...")

    migrated = 0
    no_legacy = 0
    tk_total = 0
    mal_total = 0
    skipped = 0

    for i, fname in enumerate(files):
        path = os.path.join(anime_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({fname}): {e}")
            skipped += 1
            continue

        legacy = data.get("episodes")
        if legacy is None:
            no_legacy += 1
            legacy = []

        tk_new, mal_new = storage._split_legacy_episodes(legacy)

        turkanime = data.get("turkanime")
        if not isinstance(turkanime, dict):
            turkanime = {}
        jikan = data.get("jikan")
        if not isinstance(jikan, dict):
            jikan = {}

        tk_merged = merge_by_number(turkanime.get("episodes") or [], tk_new)
        mal_merged = merge_by_number(jikan.get("episode_list") or [], mal_new)

        turkanime["episodes"] = tk_merged
        jikan["episode_list"] = mal_merged
        data["turkanime"] = turkanime
        data["jikan"] = jikan
        data.pop("episodes", None)

        tk_total += len(tk_merged)
        mal_total += len(mal_merged)
        migrated += 1

        if not dry_run:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Dosya yazılamadı ({fname}): {e}")
                skipped += 1
                continue

        if (i + 1) % 1000 == 0:
            logger.info(f"⏳ İlerleme: {i + 1}/{len(files)}")

    logger.info("==========================================")
    logger.info(f"{'🧪 [DRY-RUN] ' if dry_run else ''}Taşınan dosya: {migrated}")
    logger.info(f"Eski 'episodes' dizisi olmayan dosya: {no_legacy} | atlanan: {skipped}")
    logger.info(f"turkanime.episodes kaydı: {tk_total}")
    logger.info(f"jikan.episode_list kaydı: {mal_total}")
    logger.info("==========================================")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
