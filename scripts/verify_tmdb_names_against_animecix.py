"""
scripts/verify_tmdb_names_against_animecix.py — tmdb.name Doğrulama Testi (AnimeciX Search)

Her bir api/anime/{mal_id}.json dosyasının "tmdb" eşleşmesindeki "name" değeri
AnimeciX arama API'sinde (/secure/search/{query}) aranır. Dönen sonuçlar
arasında aynı TMDB kimliğine (tmdb_id) sahip bir kayıt bulunup bulunmadığı
kontrol edilir.

Amaç: AnimeciX isimlerinin (ve katalogda olmayanların TMDB isimlerinin) AnimeciX
arşivinde arama sonuçlarıyla doğrulanabilme oranını ölçmek.

Kullanım:
    python scripts/verify_tmdb_names_against_animecix.py
    python scripts/verify_tmdb_names_against_animecix.py --limit 50 --workers 3
    python scripts/verify_tmdb_names_against_animecix.py --only-in-catalog
"""

import os
import re
import sys
import json
import argparse
import threading
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# Proje kök dizinini ekle
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "legacy"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from animecix_provider import AnimecixProvider  # noqa: E402
from src.logger import setup_logger  # noqa: E402

logger = setup_logger("VerifyTmdbVsAnimecix")

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9d]")


def load_catalog_tmdb_ids(catalog_path: str) -> set[str]:
    """AnimeciX kataloğunda geçen tüm TMDB kimliklerini döndürür."""
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except Exception as e:
        logger.warning(f"Katalog okunamadı ({e}); filtre uygulanmayacak.")
        return set()

    ids = {str(e["tmdb_id"]) for e in catalog if e.get("tmdb_id")}
    logger.info(f"📚 AnimeciX kataloğu: {len(catalog)} başlık, {len(ids)} benzersiz tmdb_id")
    return ids


def load_entries(anime_dir: str, limit: int | None = None, catalog_ids: set[str] | None = None) -> tuple[list[dict], dict]:
    """tmdb.name dolu olan dosyaları okur.

    catalog_ids verilirse yalnızca bu kimliklerdeki dosyalar hedefe alınır
    (AnimeciX arşivinde olmayan içerikler için gereksiz istek atılmaz).
    """
    entries = []
    skipped = {"no_tmdb_block": 0, "no_name": 0, "read_error": 0, "not_in_catalog": 0}

    for fname in sorted(os.listdir(anime_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(anime_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            skipped["read_error"] += 1
            continue

        tmdb = data.get("tmdb")
        if not isinstance(tmdb, dict):
            skipped["no_tmdb_block"] += 1
            continue
        if not tmdb.get("name"):
            skipped["no_name"] += 1
            continue

        tmdb_id = str(tmdb.get("id"))
        if catalog_ids is not None and tmdb_id not in catalog_ids:
            skipped["not_in_catalog"] += 1
            continue

        entries.append({
            "file": fname,
            "mal_id": fname[:-5],
            "tmdb_id": tmdb_id,
            "tmdb_type": tmdb.get("type"),
            "name": tmdb.get("name"),
        })
        if limit and len(entries) >= limit:
            break

    return entries, skipped


def animecix_search(provider: AnimecixProvider, name: str, search_limit: int) -> tuple[list[dict], bool]:
    """
    AnimeciX araması yapar.

    Not: Başlıktaki "/" karakteri yol ayracı olduğu için (örn. "Fate/Zero")
    path segment olarak tam kodlanır (safe=""). Bu yüzden provider.search()
    yerine doğrudan _get çağrılır; aksi halde endpoint bozulur.

    Returns:
        (sonuçlar, api_hata_var_mı)
    """
    query = name.strip()
    encoded = urllib.parse.quote(query, safe="")
    data = provider._get(f"/secure/search/{encoded}", params={"limit": search_limit})
    if isinstance(data, dict):
        return data.get("results", []) or [], False

    # "/" içeren veya yanıt dönmeyen isimlerde alternatif deneme: ayraçları boşluğa çevir
    alt_query = query.replace("/", " ").replace("\\", " ").strip()
    if alt_query and alt_query != query:
        alt_encoded = urllib.parse.quote(alt_query, safe="")
        data = provider._get(f"/secure/search/{alt_encoded}", params={"limit": search_limit})
        if isinstance(data, dict):
            return data.get("results", []) or [], False
        return [], True

    return [], True


def search_once(provider: AnimecixProvider, name: str, search_limit: int) -> tuple[bool, list[dict] | None, str | None]:
    """
    Tek arama isteği. Returns:
        (istek başarılı mı, sonuç listesi veya None, hata mesajı)
    """
    try:
        results, failed = animecix_search(provider, name, search_limit)
        if failed:
            return False, None, "API yanıtı alınamadı"
        return True, results, None
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def run(limit: int | None, workers: int, search_limit: int, report_path: str, only_in_catalog: bool):
    anime_dir = os.path.join(ROOT, "api", "anime")
    catalog_path = os.path.join(ROOT, "data", "animecix_catalog.json")

    catalog_ids = load_catalog_tmdb_ids(catalog_path) if only_in_catalog else None
    entries, skipped = load_entries(anime_dir, limit, catalog_ids=catalog_ids)

    logger.info("=" * 70)
    logger.info("🔎 TMDB name ↔ AnimeciX Search doğrulama testi")
    logger.info("=" * 70)
    logger.info(f"📂 Test edilecek dosya: {len(entries)}")
    logger.info(f"   ↳ kapsam dışı → tmdb nesnesi yok: {skipped['no_tmdb_block']} | name boş: {skipped['no_name']} | "
                f"AnimeciX kataloğunda yok: {skipped['not_in_catalog']}")
    if only_in_catalog:
        logger.info("🎯 Filtre: yalnızca AnimeciX kataloğunda bulunan animeler sorgulanıyor")

    if not entries:
        logger.info("Test edilecek dosya yok.")
        return

    # Aynı (name, tmdb_id) çiftleri için tek sorgu
    unique: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        unique.setdefault((e["name"].strip().lower(), e["tmdb_id"]), []).append(e)

    logger.info(f"🔁 Benzersiz sorgu sayısı: {len(unique)} (tekrar eden isimler atlandı)")
    logger.info(f"🌐 AnimeciX araması: sorgu başına en fazla {search_limit} sonuç, {workers} paralel iş parçacığı")

    provider = AnimecixProvider()

    # (name, tmdb_id) -> sonuç listesi
    results_by_key: dict[tuple[str, str], list[dict] | None] = {}
    done = 0
    error_count = 0
    lock = threading.Lock()

    keys = list(unique.keys())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(search_once, provider, name, search_limit): (name, tmdb_id)
                   for name, tmdb_id in keys}
        for fut in as_completed(futures):
            key = futures[fut]
            ok, results, err = fut.result()
            with lock:
                results_by_key[key] = results if ok else None
                if not ok:
                    error_count += 1
            done += 1
            if done % 250 == 0 or done == len(keys):
                logger.info(f"  ⏳ {done}/{len(keys)} sorgu tamamlandı...")

    # ─────────────────────────────────────────────────────────────
    # SONUÇLARIN DEĞERLENDİRİLMESİ
    # ─────────────────────────────────────────────────────────────
    matched = []
    unmatched = []
    no_results = []
    request_errors = []

    for (name_key, tmdb_id), file_entries in unique.items():
        results = results_by_key.get((name_key, tmdb_id))
        if results is None:
            request_errors.append((name_key, tmdb_id, len(file_entries)))
            continue

        hit = next((r for r in results if str(r.get("tmdb_id")) == tmdb_id), None)

        for fe in file_entries:
            record = {
                "file": fe["file"],
                "mal_id": fe["mal_id"],
                "tmdb_id": tmdb_id,
                "tmdb_type": fe["tmdb_type"],
                "name": fe["name"],
                "result_count": len(results),
            }
            if hit:
                record["animecix"] = {
                    "id": hit.get("id"),
                    "name": hit.get("name"),
                    "name_english": hit.get("name_english"),
                    "title_type": hit.get("title_type"),
                    "year": hit.get("year"),
                }
                matched.append(record)
            else:
                if results:
                    record["top_results"] = [
                        {"id": r.get("id"), "tmdb_id": r.get("tmdb_id"), "name": r.get("name")}
                        for r in results[:5]
                    ]
                    unmatched.append(record)
                else:
                    no_results.append(record)

    total_files = len(entries)
    stats = {
        "tested_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "only_in_catalog": only_in_catalog,
        "total_files": total_files,
        "unique_queries": len(unique),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "empty_results": len(no_results),
        "request_errors": len(request_errors),
        "skipped": skipped,
        "match_rate": round((len(matched) / total_files) * 100, 2) if total_files else 0.0,
    }

    # ── Kırılımlar ──
    by_type = {}
    for label, subset in (("matched", matched), ("unmatched", unmatched), ("empty", no_results)):
        counter = Counter(r["tmdb_type"] for r in subset)
        total = sum(counter.values())
        by_type[label] = {
            "total": total,
            "tv": counter.get("tv", 0),
            "movie": counter.get("movie", 0),
            "rate": round((counter.get("tv", 0) / total) * 100, 2) if total else 0.0,
        }

    by_type_totals = Counter(e["tmdb_type"] for e in entries)
    per_type_rate = {}
    for t, cnt in by_type_totals.items():
        m = sum(1 for r in matched if r["tmdb_type"] == t)
        per_type_rate[t] = {
            "total": cnt,
            "matched": m,
            "rate": round((m / cnt) * 100, 2) if cnt else 0.0,
        }

    cjk_names = [r for r in matched if CJK_RE.search(r["name"] or "")]
    cjk_unmatched = [r for r in unmatched if CJK_RE.search(r["name"] or "")]

    # AnimeciX arşivinde hiç bulunamayan TMDB id'leri (en çok tekrar edenler)
    missing_id_counter = Counter(r["tmdb_id"] for r in unmatched + no_results)
    top_missing_ids = missing_id_counter.most_common(15)

    stats["by_type"] = per_type_rate
    stats["detail"] = by_type
    stats["cjk"] = {"matched": len(cjk_names), "unmatched": len(cjk_unmatched)}

    # ─────────────────────────────────────────────────────────────
    # RAPOR
    # ─────────────────────────────────────────────────────────────
    logger.info("=" * 70)
    logger.info("📊 SONUÇ ÖZETİ")
    logger.info("=" * 70)
    logger.info(f"Toplam test edilen dosya      : {total_files}")
    logger.info(f"Benzersiz sorgu              : {len(unique)}")
    logger.info(f"✅ Eşleşen                   : {stats['matched']}  ({stats['match_rate']}%)")
    logger.info(f"❌ Eşleşmeyen (sonuç var)    : {stats['unmatched']}")
    logger.info(f"🔍 Sonuç dönmeyen             : {stats['empty_results']}")
    logger.info(f"⚠️  İstek hatası              : {stats['request_errors']}")

    logger.info("-" * 70)
    logger.info("Tip bazlı eşleşme:")
    for t, v in per_type_rate.items():
        logger.info(f"  {str(t):6} -> {v['matched']}/{v['total']}  ({v['rate']}%)")

    logger.info("-" * 70)
    logger.info(f"CJK karakterli isimler       : eşleşen {len(cjk_names)} / eşleşmeyen {len(cjk_unmatched)}")
    logger.info("-" * 70)
    logger.info("En çok eşleşmeyen TMDB id'leri (top 15):")
    for tid, cnt in top_missing_ids:
        sample = next((r for r in unmatched + no_results if r["tmdb_id"] == tid), {})
        logger.info(f"  tmdb_id={tid:>9}  x{cnt:<4} örnek isim: {sample.get('name')}")

    logger.info("-" * 70)
    logger.info("Eşleşme ÖRNEKLERİ:")
    for r in matched[:10]:
        a = r["animecix"]
        logger.info(f"  ✅ {r['file']:>14} | {r['name'][:42]:<44} -> animecix #{a['id']} {a['name'][:32]}")
    logger.info("-" * 70)
    logger.info("Eşleşmeme ÖRNEKLERİ:")
    for r in (unmatched + no_results)[:10]:
        logger.info(f"  ❌ {r['file']:>14} | {r['name'][:42]:<44} | sonuç: {r['result_count']}")

    # ── JSON raporu ──
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    payload = {
        "summary": stats,
        "top_missing_tmdb_ids": [{"tmdb_id": tid, "count": cnt} for tid, cnt in top_missing_ids],
        "matched": matched,
        "unmatched": unmatched,
        "empty_results": no_results,
        "request_errors": request_errors,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("=" * 70)
    logger.info(f"💾 Detaylı rapor: {report_path}")
    logger.info("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="tmdb.name değerlerini AnimeciX aramasında doğrular")
    parser.add_argument("--limit", type=int, default=None, help="Yalnızca ilk N dosyayı test et.")
    parser.add_argument("--workers", type=int, default=3, help="Paralel istek sayısı (varsayılan: 3).")
    parser.add_argument("--search-limit", type=int, default=20, help="Arama başına sonuç limiti (varsayılan: 20).")
    parser.add_argument("--report", default=os.path.join(ROOT, "reports", "animecix_search_tmdb_match_report.json"),
                        help="JSON rapor çıktı yolu.")
    parser.add_argument("--only-in-catalog", action="store_true",
                        help="Yalnızca AnimeciX kataloğunda bulunan animeleri sorgular (gereksiz istekleri önler).")
    args = parser.parse_args()

    run(limit=args.limit, workers=args.workers, search_limit=args.search_limit,
        report_path=args.report, only_in_catalog=args.only_in_catalog)
