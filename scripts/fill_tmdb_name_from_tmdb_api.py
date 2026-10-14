"""
scripts/fill_tmdb_name_from_tmdb_api.py — Boş tmdb.name Alanlarının TMDB API'den Doldurulması

AnimeciX kataloğunda karşılığı bulunamayan seriler için api/anime/{mal_id}.json
dosyalarının "tmdb" eşleşmesinde bulunan TMDB kimliği (id) doğrudan TMDB API'den
sorgulanır ve dönen başlık "tmdb.name" alanına yazılır.

Not: Günlük senkronizasyon (main.py) yeni eşleşmelerde ismi otomatik çekiyor;
bu script, tarihsel olarak eksik kalanları tek seferlik toplu doldurma içindir.

Özellikler:
  1. Yalnızca "tmdb" nesnesi olan dosyalar işlenir; varsayılan olarak hedef,
     "name" alanı boş/missing olanlardır.
  2. "--refresh-names" ile mevcut isimler de yeni dil önceliğiyle tazelenir.
  3. Dil sırası: en-US -> tr-TR -> (dilsiz, original_title).
  4. data/tmdb_title_cache.json önbelleği kullanılır (src.tmdb_names); her dil
     ayrı önbelleklendiği için sadece eksik dil için API'ye gidilir.
  5. 429 (rate limit) ve geçici hatalar için otomatik yeniden deneme + backoff.

Kullanım:
    python scripts/fill_tmdb_name_from_tmdb_api.py --dry-run
    python scripts/fill_tmdb_name_from_tmdb_api.py
    python scripts/fill_tmdb_name_from_tmdb_api.py --refresh-names
"""

import os
import sys
import json
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Proje kök dizinini ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.logger import setup_logger
from src.tmdb_names import TMDBNameResolver

logger = setup_logger("FillTmdbNameFromAPI")


def collect_targets(anime_dir: str, refresh_names: bool = False) -> tuple[list[tuple[str, str, bool]], dict]:
    """
    Hedef dosyaları tarar.

    Args:
        refresh_names: True ise "name" alanı dolu olan dosyalar da hedefe dahil edilir
                       (mevcut isim yeni dil önceliğiyle tazelenir).

    Returns:
        (hedefler, istatistikler)
        hedefler: [(dosya_yolu, tmdb_id, is_movie), ...]
    """
    targets = []
    stats = {"no_tmdb_block": 0, "already_named": 0, "missing_tmdb_id": 0, "read_error": 0}

    for fname in sorted(os.listdir(anime_dir)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(anime_dir, fname)

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({fname}): {e}")
            stats["read_error"] += 1
            continue

        tmdb = data.get("tmdb")
        if not isinstance(tmdb, dict):
            stats["no_tmdb_block"] += 1
            continue

        if tmdb.get("name") and not refresh_names:
            stats["already_named"] += 1
            continue

        tmdb_id = tmdb.get("id")
        if not tmdb_id:
            stats["missing_tmdb_id"] += 1
            continue

        targets.append((path, str(tmdb_id), tmdb.get("type") == "movie"))

    return targets, stats


def run(dry_run: bool = False, workers: int = 4, refresh_names: bool = False):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    anime_dir = os.path.join(root, "api", "anime")

    resolver = TMDBNameResolver()
    if not resolver.enabled:
        logger.error("TMDB_API_KEY / TMDB_READ_TOKEN yok — işlem yapılamıyor.")
        return

    targets, stats = collect_targets(anime_dir, refresh_names=refresh_names)
    unique_ids = {(tid, is_movie) for _, tid, is_movie in targets}
    logger.info(f"📂 Toplam hedef dosya: {len(targets)} | benzersiz TMDB id: {len(unique_ids)}")
    logger.info(f"   ↳ tmdb nesnesi olmayan: {stats['no_tmdb_block']} | zaten name'li (atlanan): {stats['already_named']}")
    if refresh_names:
        logger.info("🔄 Mevcut isimler yeni dil önceliğiyle tazeleniyor (--refresh-names)")

    if not unique_ids:
        logger.info("Tamamlanacak dosya yok.")
        return

    mode = "ÖİZLEME (dry-run)" if dry_run else "UYGULANDI"
    logger.info(f"🌐 TMDB API'den {len(unique_ids)} başlık çekiliyor — {mode}")

    titles: dict[tuple[str, bool], str | None] = {}
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(resolver.fetch_title, tid, is_movie): (tid, is_movie)
            for tid, is_movie in unique_ids
        }
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                titles[key] = fut.result()
            except Exception as e:
                logger.warning(f"Başlık çekilemedi ({key}): {e}")
                titles[key] = None
            done += 1
            if done % 100 == 0:
                logger.info(f"  ⏳ {done}/{len(unique_ids)} başlık çekildi...")

    # Önbellek gerçek API yanıtlarından oluşur; dry-run'da da korunur ki
    # sonraki çalışmalarda aynı id'ler tekrar sorgulanmasın.
    resolver.save_cache()

    written = 0
    unchanged = 0
    no_title = 0
    failed_files = 0

    for path, tmdb_id, is_movie in targets:
        title = titles.get((tmdb_id, is_movie))
        if not title:
            no_title += 1
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({os.path.basename(path)}): {e}")
            failed_files += 1
            continue

        tmdb = data.get("tmdb")
        if not isinstance(tmdb, dict) or str(tmdb.get("id")) != tmdb_id:
            continue

        if tmdb.get("name") == title:
            unchanged += 1
            continue

        tmdb["name"] = title
        data["tmdb"] = tmdb
        written += 1

        if dry_run:
            continue

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Dosya yazılamadı ({os.path.basename(path)}): {e}")

    logger.info("=" * 50)
    logger.info(f"🎉 TMDB API ile tmdb.name doldurma tamamlandı — {mode}")
    logger.info(f"📝 Yazılan dosya: {written}")
    logger.info(f"➖ Değişmedi (atlandı): {unchanged}")
    logger.info(f"🔍 Başlık bulunamadı (boş bırakıldı): {no_title}")
    logger.info(f"⚠️ Yazılamayan dosya: {failed_files}")
    logger.info("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tmdb.name alanlarını TMDB API'den doldurur/tazeler")
    parser.add_argument("--dry-run", action="store_true", help="Yalnızca raporlar, dosyalara yazmaz.")
    parser.add_argument("--workers", type=int, default=4, help="Paralel istek sayısı (varsayılan: 4).")
    parser.add_argument("--refresh-names", action="store_true", help="Mevcut name alanlarını da yeni dil önceliğiyle tazeler.")
    args = parser.parse_args()

    run(dry_run=args.dry_run, workers=args.workers, refresh_names=args.refresh_names)
