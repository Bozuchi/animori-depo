"""
scripts/fill_tmdb_name_from_animecix.py — tmdb.name Alanının AnimeciX Kataloğundan Doldurulması

AnimeciX kataloğundaki her başlığın TMDB kimliği (tmdb_id) ile yerel
api/anime/{mal_id}.json dosyalarının "tmdb" eşleşmesindeki "id" alanı
karşılaştırılır ve eşleşen başlıkların AnimeciX adı ("name") ilgili dosyanın
tmdb.name alanına yazılır.

Özellikler:
  1. Yalnızca "tmdb" anahtarı geçerli bir nesne (dict) olan dosyalar işlenir;
     "tmdb": null olan dosyalara dokunulmaz.
  2. Eşleştirme tmdb.id üzerinden yapılır.
  3. Aynı tmdb_id'yi paylaşan birden fazla AnimeciX kaydı varsa (aynı dizinin
     film/OVA kayıtları gibi) önce tip (movie / series), sonra is_series ve
     bölüm sayısı üzerinden en uygun kayıt seçilir; belirsiz durumlar loglanır.
  4. Varsayılan olarak çalışma modu "--dry-run" seçeneği olmadan uygular;
     önizleme için "--dry-run", mevcut isimleri tazelemek için "--overwrite"
     kullanılır.

Kullanım:
    python scripts/fill_tmdb_name_from_animecix.py --dry-run
    python scripts/fill_tmdb_name_from_animecix.py
    python scripts/fill_tmdb_name_from_animecix.py --overwrite
"""

import os
import sys
import json
import argparse
from collections import defaultdict

# Proje kök dizinini ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.logger import setup_logger

logger = setup_logger("FillTmdbName")


def load_catalog(catalog_path: str) -> dict:
    """AnimeciX kataloğunu okur ve tmdb_id -> kayıt listesi indeksine çevirir."""
    with open(catalog_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    index: dict[str, list[dict]] = defaultdict(list)
    for entry in catalog:
        tmdb_id = entry.get("tmdb_id")
        if tmdb_id:
            index[str(tmdb_id)].append(entry)

    logger.info(f"📂 AnimeciX kataloğu yüklendi: {len(catalog)} başlık ({len(index)} benzersiz tmdb_id).")
    return index


def is_movie_type(entry: dict) -> bool:
    """AnimeciX kaydı film mi (title_type/type alanlarından)."""
    if entry.get("type") == "movie":
        return True
    return entry.get("title_type") == "movie"


def pick_entry(candidates: list[dict], tmdb_type: str, season: int) -> dict | None:
    """
    Aynı tmdb_id'ye sahip birden fazla AnimeciX kaydı arasından en uygun olanı seçer.

    Öncelik sırası:
      1. Dosyanın tmdb tipiyle (movie / tv) uyumlu kayıtlar
      2. tv için is_series == True olan kayıtlar
      3. tv için season_count >= dosya sezonu olan kayıtlar
      4. Bölüm sayısı en fazla olan kayıt (ana yapım genelde en kapsamlısı)
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    want_movie = tmdb_type == "movie"
    typed = [c for c in candidates if is_movie_type(c) == want_movie]
    pool = typed or candidates

    if len(pool) > 1 and not want_movie:
        series_pool = [c for c in pool if c.get("is_series")]
        if series_pool:
            pool = series_pool

    if len(pool) > 1:
        season = season or 1
        season_pool = [c for c in pool if (c.get("season_count") or 0) >= season]
        if season_pool:
            pool = season_pool

    return max(pool, key=lambda c: (c.get("episode_count") or 0, c.get("id") or 0))


def run(dry_run: bool = False, overwrite: bool = False):
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    anime_dir = os.path.join(root, "api", "anime")
    catalog_path = os.path.join(root, "data", "animecix_catalog.json")

    if not os.path.exists(catalog_path):
        logger.error(f"AnimeciX kataloğu bulunamadı: {catalog_path}")
        return
    if not os.path.isdir(anime_dir):
        logger.error(f"Anime dizini bulunamadı: {anime_dir}")
        return

    catalog_index = load_catalog(catalog_path)

    json_files = [f for f in sorted(os.listdir(anime_dir)) if f.endswith(".json")]
    logger.info(f"📂 {len(json_files)} yerel anime dosyası taranıyor...")

    stats = {
        "updated": 0,
        "already_set": 0,
        "no_tmdb_block": 0,
        "no_catalog_match": 0,
        "ambiguous": 0,
        "read_error": 0,
    }
    ambiguous_examples = []
    unmatched_examples = []

    for fname in json_files:
        file_path = os.path.join(anime_dir, fname)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"Dosya okunamadı ({fname}): {e}")
            stats["read_error"] += 1
            continue

        tmdb = data.get("tmdb")
        if not isinstance(tmdb, dict):
            # "tmdb": null veya eksik -> dokunulmaz
            stats["no_tmdb_block"] += 1
            continue

        tmdb_id = tmdb.get("id")
        if not tmdb_id:
            stats["no_catalog_match"] += 1
            continue

        candidates = catalog_index.get(str(tmdb_id), [])
        if not candidates:
            stats["no_catalog_match"] += 1
            if len(unmatched_examples) < 10:
                unmatched_examples.append((fname, str(tmdb_id)))
            continue

        if len(candidates) > 1:
            stats["ambiguous"] += 1

        entry = pick_entry(candidates, tmdb.get("type", "tv"), tmdb.get("season") or 1)
        if entry is None:
            stats["no_catalog_match"] += 1
            continue

        name = entry.get("name")
        if not name:
            stats["no_catalog_match"] += 1
            continue

        if len(candidates) > 1 and len(ambiguous_examples) < 10:
            ambiguous_examples.append((fname, str(tmdb_id), [c.get("name") for c in candidates], name))

        current = tmdb.get("name")
        if current == name:
            stats["already_set"] += 1
            continue
        if current and not overwrite:
            stats["already_set"] += 1
            continue

        tmdb["name"] = name
        data["tmdb"] = tmdb

        if dry_run:
            stats["updated"] += 1
            continue

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            stats["updated"] += 1
        except Exception as e:
            logger.error(f"Dosya yazılamadı ({fname}): {e}")

    logger.info("=" * 50)
    mode = "ÖİZLEME (dry-run)" if dry_run else "UYGULANDI"
    logger.info(f"🎉 tmdb.name doldurma tamamlandı — {mode}")
    logger.info(f"📝 Yazılacak/güncellenecek dosya: {stats['updated']}")
    logger.info(f"✔️ Zaten eşleşen (atlandı): {stats['already_set']}")
    logger.info(f"🔍 tmdb nesnesi olmayan (atlandı): {stats['no_tmdb_block']}")
    logger.info(f"❓ Katalogda karşılığı bulunamayan: {stats['no_catalog_match']}")
    logger.info(f"⚠️ Birden fazla katalog kaydı olan: {stats['ambiguous']}")
    logger.info(f"⚠️ Okunamayan dosya: {stats['read_error']}")

    if ambiguous_examples:
        logger.info("--- Çoklu kayıt örnekleri (seçilen: | diğerleri) ---")
        for fname, tid, names, chosen in ambiguous_examples:
            logger.info(f"  {fname} tmdb_id={tid} -> seçilen: {chosen} | diğerleri: {names}")

    if unmatched_examples:
        logger.info("--- Eşleşmeyen örnekler ---")
        for fname, tid in unmatched_examples:
            logger.info(f"  {fname} tmdb_id={tid}")
    logger.info("=" * 50)

    if dry_run:
        logger.info("ℹ️  Gerçek uygulama için --dry-run bayrağını kullanmadan çalıştırın.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tmdb.name alanını AnimeciX kataloğundan doldurur")
    parser.add_argument("--dry-run", action="store_true", help="Yalnızca raporlar, dosyalara yazmaz.")
    parser.add_argument("--overwrite", action="store_true", help="Dolu olan mevcut name alanlarını da günceller.")
    args = parser.parse_args()

    run(dry_run=args.dry_run, overwrite=args.overwrite)
