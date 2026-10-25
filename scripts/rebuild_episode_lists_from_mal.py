"""
scripts/rebuild_episode_lists_from_mal.py — jikan.episode_list'i MAL'den yeniden doldurur

Migrasyon sonrası jikan.episode_list eski birleşik diziden türetilmiş olabilir.
Bu script tüm arşivi (veya sadece aktifleri) MyAnimeList bölüm listesinden
yeniden çeker; turkanime.episodes alanına dokunmaz.

Özellikler:
- Yeniden başlatılabilir (data/episode_list_backfill.json checkpoint'i)
- Sayfa başına MAL istekleri arasında mal_scraper'ın kendi rate-limit gecikmesi var
- Boş gelen MAL listeleri mevcut veriyi ezmez (merge_and_save_episodes koruması)

Kullanım:
    python scripts/rebuild_episode_lists_from_mal.py --only-active
    python scripts/rebuild_episode_lists_from_mal.py --limit 100
    python scripts/rebuild_episode_lists_from_mal.py                 # tüm arşiv
    python scripts/rebuild_episode_lists_from_mal.py --reset         # checkpoint'i sıfırla
"""

import os
import sys
import json
import time
import argparse

# Proje kök dizinini Python path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage_manager import StorageManager
from src.mal_scraper import MalHtmlEnricher
from src.logger import setup_logger

logger = setup_logger("RebuildEpisodeLists")

CHECKPOINT_PATH = os.path.join("data", "episode_list_backfill.json")
DELAY_BETWEEN_ANIME = 0.5


def load_checkpoint() -> set[int]:
    if not os.path.exists(CHECKPOINT_PATH):
        return set()
    try:
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("done") or [])
    except Exception as e:
        logger.warning(f"Checkpoint okunamadı, sıfırdan başlanıyor: {e}")
        return set()


def save_checkpoint(done: set[int]):
    try:
        os.makedirs(os.path.dirname(CHECKPOINT_PATH), exist_ok=True)
        with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump({"done": sorted(done), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Checkpoint yazılamadı: {e}")


def get_active_ids(storage: StorageManager) -> set[int]:
    """animes.json'dan yayında / henüz başlamamış animelerin MAL ID'lerini toplar."""
    index_path = os.path.join(storage.base_dir, "animes.json")
    active = set()
    if not os.path.exists(index_path):
        return active
    with open(index_path, "r", encoding="utf-8") as f:
        items = json.load(f)
    for item in items:
        mid = item.get("mal_id")
        status = item.get("status")
        if mid and (status in ("Currently Airing", "Not yet aired")
                    or (status and status != "Finished Airing")):
            active.add(int(mid))
    return active


def run(args):
    storage = StorageManager()
    mal = MalHtmlEnricher(metadata=storage.metadata)

    done = set() if args.reset else load_checkpoint()
    active_ids = get_active_ids(storage) if args.only_active else None

    files = sorted(f for f in os.listdir(storage.anime_dir) if f.endswith(".json"))
    targets = []
    for fname in files:
        base = fname[:-5]
        if not base.isdigit():
            continue
        mid = int(base)
        if active_ids is not None and mid not in active_ids:
            continue
        if mid in done:
            continue
        targets.append(mid)

    if args.limit:
        targets = targets[:args.limit]

    logger.info(f"📋 İşlenecek anime: {len(targets)} | checkpoint'te hazır: {len(done)}")

    updated = 0
    empty = 0
    failed = 0

    for i, mid in enumerate(targets, start=1):
        try:
            episodes = mal.fetch_episodes(mid)
        except Exception as e:
            logger.warning(f"Bölüm listesi çekilemedi ({mid}): {e}")
            episodes = {}
            failed += 1

        if episodes:
            if not args.dry_run:
                storage.merge_and_save_episodes(mid, episodes)
            updated += 1
        else:
            empty += 1
            logger.debug(f"MAL bölüm listesi boş: {mid}")

        done.add(mid)

        if i % 25 == 0:
            if not args.dry_run:
                save_checkpoint(done)
            logger.info(f"⏳ İlerleme: {i}/{len(targets)} | güncellenen: {updated} | boş: {empty} | hata: {failed}")

        time.sleep(DELAY_BETWEEN_ANIME)

    if not args.dry_run:
        save_checkpoint(done)

    logger.info("==========================================")
    logger.info(f"{'🧪 [DRY-RUN] ' if args.dry_run else ''}Tamamlandı.")
    logger.info(f"Güncellenen: {updated} | MAL listesi boş: {empty} | hata: {failed}")
    logger.info("İndeksi yenilemek için: python main.py --build-index")
    logger.info("==========================================")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="jikan.episode_list'i MAL'den yeniden doldurur.")
    parser.add_argument("--only-active", action="store_true", help="Sadece yayında/başlamamış animeleri işler.")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum anime sayısı.")
    parser.add_argument("--dry-run", action="store_true", help="Dosyalara yazmadan dener.")
    parser.add_argument("--reset", action="store_true", help="Checkpoint'i sıfırlar.")
    run(parser.parse_args())
