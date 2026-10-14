"""
main.py — Animori Depo Veritabanı Orkestratörü (AniList & MAL Hibrit)

Bu script, animeleri AniList GraphQL API üzerinden keşfeder,
MyAnimeList üzerinden detay ve bölüm listelerini zenginleştirir,
özetleri Türkçeye çevirir ve statik JSON veritabanını günceller.

Kullanım:
    python main.py                  # Günlük akıllı senkronizasyon (devam eden ve güncellenen seriler)
    python main.py --sync-season    # Mevcut sezon animelerini keşfeder ve senkronize eder
    python main.py --full           # Genişletilmiş tarama (daha fazla sayfa)
    python main.py --limit 10       # Test için maksimum başlık sayısı
    python main.py --build-index    # Yalnızca indeksleri (animes.json, version.json vb.) yeniden kurar
"""

import os
import sys
import json
import signal
import argparse
from datetime import datetime
from typing import Optional

from src.logger import setup_logger
from src.anilist_client import AnilistClient
from src.mal_scraper import MalHtmlEnricher
from src.storage_manager import StorageManager
from src.translator import translate_synopsis
from src.discord_notify import send_report
from src.fribb_mapper import FribbMapper
from src.tmdb_names import TMDBNameResolver

logger = setup_logger("Main")

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("İkinci durdurma sinyali alındı, zorla çıkılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Durdurma isteği alındı! Mevcut anime tamamlandıktan sonra güvenle kaydedilip çıkılacak...")


def _format_elapsed(elapsed) -> str:
    total_seconds = int(elapsed.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}s")
    if minutes > 0:
        parts.append(f"{minutes}dk")
    parts.append(f"{seconds}sn")
    return " ".join(parts) or "0sn"


def sync_anime_item(
    item: dict,
    anilist_client: AnilistClient,
    mal_scraper: MalHtmlEnricher,
    storage: StorageManager,
    skip_translate: bool = False,
    fribb_mapper: Optional[FribbMapper] = None,
    tmdb_names: Optional[TMDBNameResolver] = None,
) -> dict:
    """
    Tek bir anime kaydını inceler, gerekirse oluşturur veya günceller.
    
    Returns:
        dict: {"is_new": bool, "new_episodes": int, "updated": bool}
    """
    mal_id = item.get("idMal")
    if not mal_id:
        return {"is_new": False, "new_episodes": 0, "updated": False}

    existing = storage.load_anime_detail(mal_id)
    title = (
        item.get("title_romaji")
        or item.get("title_english")
        or (existing.get("jikan", {}).get("title") if existing else None)
        or (existing.get("turkanime", {}).get("isim") if existing else None)
        or f"MAL-{mal_id}"
    )

    # AniList status eksikse (örn. yerel taramadan geldiyse) AniList'ten doldur
    if not item.get("status"):
        ani_data = anilist_client.fetch(mal_id)
        if ani_data:
            item.update(ani_data)

    # ─────────────────────────────────────────────────────────────
    # DURUM 1: TAMAMEN YENİ ANİME
    # ─────────────────────────────────────────────────────────────
    if existing is None:
        # Eski bitmiş animelerin kataloğa eklenmesini engelle; sadece aktif yayında olanları ekle
        status = item.get("status")
        if status and status not in ("RELEASING", "NOT_YET_RELEASED"):
            logger.debug(f"⏭️  Eski/bitmiş seri kataloğa eklenmiyor: {title} (Durum: {status})")
            return {"is_new": False, "new_episodes": 0, "updated": False}

        logger.info(f"✨ [YENİ ANİME] Keşfedildi: {title} (MAL ID: {mal_id})")
        mal_details = mal_scraper.get_full_details(mal_id)
        if not mal_details:
            logger.warning(f"MAL detayları çekilemedi, atlanıyor: {title} ({mal_id})")
            return {"is_new": False, "new_episodes": 0, "updated": False}

        # AniList görsel ve ID bilgisi
        anilist_data = {
            "id": item.get("id"),
            "banner_image": item.get("banner_image"),
            "poster_url": item.get("poster_url"),
        }

        # Türkçe özet çevirisi
        synopsis_tr = None
        eng_synopsis = mal_details.get("synopsis")
        if eng_synopsis and not skip_translate:
            logger.info(f"🌐 Özet Türkçeye çevriliyor: {title}")
            synopsis_tr = translate_synopsis(eng_synopsis)

        # Slug oluştur (güvenli)
        slug = f"anime-{mal_id}"
        if mal_details.get("title"):
            clean_slug = "".join(c if c.isalnum() else "-" for c in mal_details["title"].lower()).strip("-")
            clean_slug = "-".join(filter(None, clean_slug.split("-")))
            if clean_slug:
                slug = clean_slug

        # TMDB / Fribb eşleşmesi (+ TMDB API'den başlık adı)
        tmdb_data = fribb_mapper.get_tmdb_info(mal_id) if fribb_mapper else None
        if tmdb_data and tmdb_names:
            tmdb_data = tmdb_names.resolve(tmdb_data)

        # Anime detayını kaydet
        storage.save_anime_detail(
            slug=slug,
            turkanime_data={"isim": mal_details.get("title") or title, "slug": slug},
            jikan_data=mal_details,
            anilist_data=anilist_data,
            synopsis_tr=synopsis_tr,
            mal_id=mal_id,
            tmdb_data=tmdb_data,
        )

        # Bölümleri çek ve birleştir
        episodes = mal_scraper.fetch_episodes(mal_id)
        new_ep_count = storage.merge_and_save_episodes(mal_id, episodes, slug=slug)

        return {"is_new": True, "new_episodes": new_ep_count, "updated": True}

    # ─────────────────────────────────────────────────────────────
    # DURUM 2: MEVCUT ANİME GÜNCELLEMESİ
    # ─────────────────────────────────────────────────────────────
    updated = False
    new_ep_count = 0

    # 1. Eksik synopsis_tr kontrolü
    if not existing.get("synopsis_tr") and not skip_translate:
        jikan = existing.get("jikan") or {}
        eng_synopsis = jikan.get("synopsis")
        if eng_synopsis:
            logger.info(f"🌐 Eksik Türkçe özet çevriliyor: {title}")
            syn_tr = translate_synopsis(eng_synopsis)
            if syn_tr:
                existing["synopsis_tr"] = syn_tr
                updated = True

    # 2. AniList eksik poster veya banner kontrolü
    existing_ani = existing.get("anilist") or {}
    ani_updated = False
    if item.get("poster_url") and not existing_ani.get("poster_url"):
        existing_ani["poster_url"] = item.get("poster_url")
        ani_updated = True
    if item.get("banner_image") and not existing_ani.get("banner_image"):
        existing_ani["banner_image"] = item.get("banner_image")
        ani_updated = True
    if item.get("id") and not existing_ani.get("id"):
        existing_ani["id"] = item.get("id")
        ani_updated = True
    if ani_updated:
        existing["anilist"] = existing_ani
        updated = True

    # 3. Güncel Puan, Yayın Durumu (Airing/Finished/Not yet aired) ve Bölüm Kontrolü
    jikan = existing.get("jikan") or {}
    was_airing = jikan.get("airing", False) or jikan.get("status") == "Currently Airing"
    was_not_yet_aired = jikan.get("status") == "Not yet aired"
    was_active = was_airing or was_not_yet_aired or (jikan.get("status") and jikan.get("status") != "Finished Airing")
    anilist_status = item.get("status")  # RELEASING, FINISHED, NOT_YET_RELEASED vb.

    # Eğer anime aktifse (yayında veya henüz başlamamış) veya yeni bittiyse (FINISHED) veya son güncellenenler listesindeyse:
    should_refresh_details = was_active or anilist_status in ("RELEASING", "FINISHED", "NOT_YET_RELEASED")

    if should_refresh_details:
        mal_details = mal_scraper.get_full_details(mal_id)
        if mal_details:
            # Puan güncellemesi
            old_score = jikan.get("score")
            new_score = mal_details.get("score")
            if new_score is not None and new_score != old_score:
                logger.info(f"⭐ Puan güncellendi: {title} ({old_score} -> {new_score})")
                jikan["score"] = new_score
                updated = True

            # Yayın durumu güncellemesi (Currently Airing -> Finished Airing)
            old_status = jikan.get("status")
            new_status = mal_details.get("status")
            new_airing = mal_details.get("airing", False)
            if new_status and new_status != old_status:
                logger.info(f"🏁 Yayın durumu güncellendi: {title} ({old_status} -> {new_status})")
                jikan["status"] = new_status
                jikan["airing"] = new_airing
                updated = True

            # Toplam bölüm sayısı güncellemesi
            new_ep_total = mal_details.get("episodes")
            if new_ep_total and new_ep_total != jikan.get("episodes"):
                jikan["episodes"] = new_ep_total
                updated = True

            # Popülerlik güncellemesi
            if mal_details.get("popularity"):
                jikan["popularity"] = mal_details["popularity"]

            existing["jikan"] = jikan

        # Bölüm listesi kontrolü (devam eden veya yeni biten seriler için)
        existing_eps_count = len(existing.get("episodes") or [])
        mal_episodes = mal_scraper.fetch_episodes(mal_id)
        if len(mal_episodes) > existing_eps_count:
            logger.info(f"📢 Yeni bölüm bulundu! {title}: {existing_eps_count} -> {len(mal_episodes)}")
            new_ep_count = storage.merge_and_save_episodes(mal_id, mal_episodes)
            updated = True

    # 4. Eksik TMDB eşleşmesi kontrolü (yeni eşleşmelerde isim de TMDB API'den alınır)
    if fribb_mapper and ("tmdb" not in existing or existing.get("tmdb") is None):
        tmdb_data = fribb_mapper.get_tmdb_info(mal_id)
        if tmdb_data:
            if tmdb_names:
                tmdb_data = tmdb_names.resolve(tmdb_data)
            existing["tmdb"] = tmdb_data
            updated = True

    # Değişen meta verileri (puan, durum, synopsis_tr, anilist, tmdb) diske kaydet
    if updated:
        storage.save_anime_detail(
            slug=existing.get("turkanime", {}).get("slug") or str(mal_id),
            turkanime_data=existing.get("turkanime"),
            jikan_data=existing.get("jikan"),
            anilist_data=existing.get("anilist"),
            synopsis_tr=existing.get("synopsis_tr"),
            mal_id=mal_id,
            tmdb_data=existing.get("tmdb"),
        )

    return {"is_new": False, "new_episodes": new_ep_count, "updated": updated}


def get_locally_active_mal_ids(storage: StorageManager) -> list[int]:
    """
    Yerel veritabanında (api/anime/*.json) henüz final yapmamış
    (Currently Airing veya Not yet aired / yayınlanması beklenen)
    animelerin MAL ID'lerini toplar.

    Öncelikle api/animes.json vitrin indeksinden anında (50ms) çeker,
    indeks yoksa veya okunamazsa dosya sistemini tarar.
    """
    index_path = os.path.join(storage.base_dir, "animes.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            active_ids = []
            for item in items:
                status = item.get("status")
                mid = item.get("mal_id")
                if mid and (status in ("Currently Airing", "Not yet aired") or (status and status != "Finished Airing")):
                    active_ids.append(mid)
            return active_ids
        except Exception as e:
            logger.warning(f"animes.json hızlı okuma başarısız, klasör taramasına geçiliyor: {e}")

    active_ids = []
    if os.path.exists(storage.anime_dir):
        for fname in sorted(os.listdir(storage.anime_dir)):
            if not fname.endswith(".json"):
                continue
            base = fname[:-5]
            if not base.isdigit():
                continue
            mid = int(base)
            data = storage.load_anime_detail(mid)
            if not data:
                continue
            jikan = data.get("jikan") or {}
            status = jikan.get("status")
            airing = jikan.get("airing")
            # Bitenler (Finished Airing) hariç: yayında olanlar veya henüz başlamayanlar
            if airing is True or status in ("Currently Airing", "Not yet aired") or (status and status != "Finished Airing"):
                active_ids.append(mid)
    return active_ids


# Geriye dönük uyumluluk için alias
get_locally_airing_mal_ids = get_locally_active_mal_ids


def run_sync(args, stats: dict):
    global _shutdown_requested
    anilist = AnilistClient()
    storage = StorageManager()
    mal = MalHtmlEnricher(metadata=storage.metadata)
    fribb = FribbMapper()
    tmdb_names = TMDBNameResolver()

    # ─────────────────────────────────────────────────────────────
    # 0. FRIBB GÜNCELLEME KONTROLÜ (ETag / DELTA SENKRONİZASYON)
    # ─────────────────────────────────────────────────────────────
    is_fribb_updated, diff_mal_ids = fribb.check_and_update_remote()
    if is_fribb_updated and diff_mal_ids:
        logger.info(f"🔄 Fribb dataseti güncellendi! {len(diff_mal_ids)} animenin TMDB bilgisi yenileniyor...")
        fribb_updated_count = 0
        for diff_id in diff_mal_ids:
            tmdb_info = tmdb_names.resolve(fribb.get_tmdb_info(diff_id))
            if storage.update_anime_tmdb(diff_id, tmdb_info):
                fribb_updated_count += 1
        logger.info(f"✅ {fribb_updated_count} animenin TMDB eşleşmesi diske kaydedildi.")
        stats["fribb_guncellenen"] = fribb_updated_count
        tmdb_names.save_cache()

    # ─────────────────────────────────────────────────────────────
    # OPSİYONEL: --sync-fribb İLE TÜM YEREL ARŞİVİ EŞLEŞTİR
    # ─────────────────────────────────────────────────────────────
    if getattr(args, "sync_fribb", False):
        logger.info("🗺️  Tüm yerel anime dosyaları Fribb TMDB eşleşmesi için taranıyor (--sync-fribb)...")
        backfill_count = 0
        total_checked = 0
        for filename in sorted(os.listdir(storage.anime_dir)):
            if not filename.endswith(".json"):
                continue
            name_no_ext = filename[:-5]
            if not name_no_ext.isdigit():
                continue
            mid = int(name_no_ext)
            total_checked += 1
            tmdb_info = tmdb_names.resolve(fribb.get_tmdb_info(mid))
            if tmdb_info:
                if storage.update_anime_tmdb(mid, tmdb_info):
                    backfill_count += 1
        logger.info(f"✅ Fribb taraması tamamlandı: {total_checked} dosyadan {backfill_count} tanesi güncellendi/kaydedildi.")
        stats["fribb_backfill_guncellenen"] = backfill_count
        tmdb_names.save_cache()
        return

    if args.build_index:
        logger.info("🔨 Vitrin indeksleri yeniden inşa ediliyor...")
        count = storage.build_index()
        storage.save_metadata()
        storage.update_versions()
        stats["toplam_indeks_anime"] = count
        logger.info(f"✅ İndeksleme tamamlandı. Toplam: {count} anime.")
        return

    items_to_sync = []
    seen_ids = set()

    # ─────────────────────────────────────────────────────────────
    # MOD 1: SADECE YEREL AKTİF (YAYINDA VEYA BEKLEYEN) OLANLARI KONTROL ET
    # ─────────────────────────────────────────────────────────────
    if args.check_airing:
        logger.info("🔍 Yerel veritabanında aktif (yayında veya henüz başlamamış) animeler taranıyor...")
        active_ids = get_locally_active_mal_ids(storage)
        logger.info(f"📊 Toplam {len(active_ids)} adet yerel aktif anime bulundu.")
        for mid in active_ids:
            if mid not in seen_ids:
                seen_ids.add(mid)
                items_to_sync.append({"idMal": mid})

    # ─────────────────────────────────────────────────────────────
    # MOD 2: STANDART CANLI RADAR + YEREL AKTİFLER SENKRONİZASYONU
    # ─────────────────────────────────────────────────────────────
    else:
        # A) Yerel depomuzda hala yayında veya henüz başlamamış serileri ekle (Bitenler ve başlayanlar ASLA kaçmaz!)
        local_active = get_locally_active_mal_ids(storage)
        logger.info(f"🔍 Yerel depomuzda yayında veya henüz başlamamış {len(local_active)} anime durum kontrolü için listeye alındı.")
        for mid in local_active:
            if mid not in seen_ids:
                seen_ids.add(mid)
                items_to_sync.append({"idMal": mid})

        max_pages = 5 if args.full else 2

        # B) AniList devam eden (RELEASING) animeler
        logger.info(f"📡 AniList'ten devam eden seriler çekiliyor (Maks sayfa: {max_pages})...")
        for p in range(1, max_pages + 1):
            if _shutdown_requested:
                break
            media_list, has_next = anilist.fetch_releasing(page=p, per_page=50)
            for m in media_list:
                mid = m.get("idMal")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    items_to_sync.append(m)
            if not has_next:
                break



        # 3. İsteğe bağlı: Mevcut Sezon animeleri
        if args.sync_season:
            current_year = datetime.now().year
            month = datetime.now().month
            if month in (1, 2, 3):
                season = "WINTER"
            elif month in (4, 5, 6):
                season = "SPRING"
            elif month in (7, 8, 9):
                season = "SUMMER"
            else:
                season = "FALL"

            logger.info(f"🍂 Mevcut sezon animeleri çekiliyor ({season} {current_year})...")
            for p in range(1, 4):
                if _shutdown_requested:
                    break
                media_list, has_next = anilist.fetch_season(season, current_year, page=p, per_page=50)
                for m in media_list:
                    mid = m.get("idMal")
                    if mid and mid not in seen_ids:
                        seen_ids.add(mid)
                        items_to_sync.append(m)
                if not has_next:
                    break



    if args.limit:
        items_to_sync = items_to_sync[:args.limit]

    logger.info(f"📋 Toplam senkronize edilecek aday sayısı: {len(items_to_sync)}")

    total_new = 0
    total_new_eps = 0
    total_updated = 0

    for idx, item in enumerate(items_to_sync, start=1):
        if _shutdown_requested:
            logger.info("⏹️  Durdurma sinyali nedeniyle döngü sonlandırıldı.")
            break

        mid = item.get("idMal")
        res = sync_anime_item(
            item=item,
            anilist_client=anilist,
            mal_scraper=mal,
            storage=storage,
            skip_translate=args.skip_translate,
            fribb_mapper=fribb,
            tmdb_names=tmdb_names,
        )

        if res["is_new"]:
            total_new += 1
        if res["new_episodes"] > 0:
            total_new_eps += res["new_episodes"]
        if res["updated"]:
            total_updated += 1

        if idx % 10 == 0 or idx == len(items_to_sync):
            logger.info(f"İlerleme: {idx}/{len(items_to_sync)} anime işlendi.")

    # İndeksleri güncelle
    logger.info("💾 İndeksler ve metadata güncelleniyor...")
    storage.save_metadata()
    storage.build_index()
    storage.update_versions()
    tmdb_names.save_cache()

    stats["yeni_eklenen_anime"] = total_new
    stats["yeni_eklenen_bolum"] = total_new_eps
    stats["guncellenen_anime"] = total_updated
    stats["toplam_islenen"] = len(items_to_sync)


def main():
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="Animori Depo Anime Senkronizasyon Orkestratörü")
    parser.add_argument("--full", action="store_true", help="Daha fazla sayfa tarayarak genişletilmiş senkronizasyon yapar.")
    parser.add_argument("--sync-season", action="store_true", help="Mevcut sezon animelerini AniList'ten çeker.")
    parser.add_argument("--check-airing", action="store_true", help="Yalnızca yerel veritabanında aktif (yayında veya henüz yayınlanmamış) animelerin güncel durumunu (Finished/Airing) ve bölümlerini denetler.")
    parser.add_argument("--sync-fribb", action="store_true", help="Tüm yerel anime dosyalarını Fribb TMDB eşleşmesi için baştan sona tarar ve günceller.")
    parser.add_argument("--limit", type=int, default=None, help="Test için işlenecek maksimum anime sayısı.")
    parser.add_argument("--build-index", action="store_true", help="Yalnızca animes.json ve diğer vitrin indekslerini yeniden oluşturur.")
    parser.add_argument("--skip-translate", action="store_true", help="Özet çevirilerini atlar (hızlı test için).")

    args = parser.parse_args()

    start_time = datetime.now()
    stats = {}
    crash_error = None

    logger.info("=" * 65)
    logger.info(f"🎌 ANİMORİ DEPO SENKRONİZASYON BAŞLADI ({start_time.strftime('%Y-%m-%d %H:%M:%S')})")
    logger.info("=" * 65)

    try:
        run_sync(args, stats)
    except Exception as e:
        crash_error = f"{type(e).__name__}: {str(e)}"
        logger.error(f"💥 Kritik çökme hatası: {crash_error}", exc_info=True)
    finally:
        elapsed = datetime.now() - start_time
        elapsed_str = _format_elapsed(elapsed)
        logger.info("=" * 65)
        logger.info(f"🏁 İŞLEM TAMAMLANDI — Süre: {elapsed_str}")
        logger.info("=" * 65)

        try:
            send_report(stats=stats, elapsed=elapsed_str, crash_error=crash_error)
        except Exception as e:
            logger.warning(f"Discord bildirimi gönderilemedi: {e}")


if __name__ == "__main__":
    main()
