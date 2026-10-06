"""
translate_all_synopses.py — Mevcut Anime Arşivini Toplu Çevirme Scripti

api/anime/ dizinindeki tüm anime detay dosyalarını inceler:
- synopsis_tr alanı henüz bulunmayan dosyaları bulur.
- jikan.synopsis (İngilizce MAL özeti) alanını deep-translator / Google Translate ile Türkçeye çevirir.
- synopsis_tr alanını güncelleyerek dosyayı kaydeder.
- Checkpoint desteği ile kesilse dahi kaldığı yerden güvenle devam eder.

Kullanım:
    python translate_all_synopses.py                 # Tüm eksikleri çevirir
    python translate_all_synopses.py --limit 100     # İlk 100 eksik animeyi çevirir
    python translate_all_synopses.py --delay 0.5     # İstekler arası bekleme (varsayılan: 0.5s)
    python translate_all_synopses.py --dry-run       # Dosyaları değiştirmeden simüle eder
"""

import os
import sys
import json
import time
import signal
import argparse
from datetime import datetime

from src.logger import setup_logger
from src.translator import translate_synopsis

logger = setup_logger("BulkTranslate")

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Zorla kapatılıyor...")
        os._exit(1)
    _shutdown_requested = True
    logger.warning("⏹️  Durdurma sinyali alındı! Mevcut çeviri tamamlandıktan sonra çıkılacak...")


def load_checkpoint(checkpoint_file: str) -> set:
    if not os.path.exists(checkpoint_file):
        return set()
    try:
        with open(checkpoint_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        logger.warning(f"Checkpoint okunamadı: {e}")
        return set()


def save_checkpoint(checkpoint_file: str, processed_ids: set):
    os.makedirs(os.path.dirname(checkpoint_file) or ".", exist_ok=True)
    try:
        with open(checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(list(processed_ids), f)
    except Exception as e:
        logger.warning(f"Checkpoint kaydedilemedi: {e}")


def main():
    global _shutdown_requested
    signal.signal(signal.SIGINT, _handle_shutdown)

    parser = argparse.ArgumentParser(description="Mevcut Animelerin Özetlerini Toplu Çevir")
    parser.add_argument("--limit", type=int, default=None, help="İşlenecek maksimum anime sayısı")
    parser.add_argument("--delay", type=float, default=0.4, help="İstekler arası bekleme süresi (saniye)")
    parser.add_argument("--checkpoint-file", type=str, default="data/translate_checkpoint.json", help="Checkpoint dosya yolu")
    parser.add_argument("--dry-run", action="store_true", help="Gerçek dosya yazımı yapmadan çalıştır")

    args = parser.parse_args()

    anime_dir = os.path.join("api", "anime")
    if not os.path.exists(anime_dir):
        logger.error(f"Anime dizini bulunamadı: {anime_dir}")
        return

    processed_ids = load_checkpoint(args.checkpoint_file)
    logger.info(f"💾 Checkpoint'ten {len(processed_ids)} işlenmiş kayıt yüklendi.")

    # Tüm dosyaları tara ve çevrilmesi gerekenleri listele
    all_files = sorted(os.listdir(anime_dir))
    candidates = []

    logger.info("🔍 Çeviriye ihtiyaç duyan animeler taranıyor...")
    for filename in all_files:
        if not filename.endswith(".json"):
            continue

        base_id = filename[:-5]
        if base_id in processed_ids:
            continue

        filepath = os.path.join(anime_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        # synopsis_tr zaten dolu mu?
        if data.get("synopsis_tr") and data["synopsis_tr"].strip():
            processed_ids.add(base_id)
            continue

        # jikan.synopsis var mı?
        jikan_synopsis = (data.get("jikan") or {}).get("synopsis")
        if not jikan_synopsis or not jikan_synopsis.strip():
            # Çevrilecek İngilizce özet yok, atla
            processed_ids.add(base_id)
            continue

        candidates.append((base_id, filename, filepath, jikan_synopsis, data))

    total_candidates = len(candidates)
    logger.info(f"📊 Toplam çevrilecek anime sayısı: {total_candidates}")

    if args.limit:
        candidates = candidates[:args.limit]
        logger.info(f"🎯 --limit uygulandı: {len(candidates)} anime işlenecek.")

    success_count = 0
    fail_count = 0
    start_time = time.time()

    for idx, (base_id, filename, filepath, eng_text, data) in enumerate(candidates, start=1):
        if _shutdown_requested:
            logger.info("⏹️  Durdurma sinyali alındı, döngüden çıkılıyor...")
            break

        title = (data.get("jikan") or {}).get("title") or base_id
        logger.info(f"[{idx}/{len(candidates)}] 🌐 Çevriliyor: {title} ({base_id})")

        if args.dry_run:
            logger.info(f"   [DRY-RUN] Çeviri atlandı.")
            success_count += 1
            continue

        try:
            syn_tr = translate_synopsis(eng_text)
            if syn_tr:
                data["synopsis_tr"] = syn_tr
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                processed_ids.add(base_id)
                success_count += 1
            else:
                logger.warning(f"   ⚠️ Çeviri boş döndü: {title}")
                fail_count += 1

        except Exception as e:
            logger.error(f"   ❌ Çeviri hatası ({title}): {e}")
            fail_count += 1

        # Checkpoint periyodik kaydet (her 25 animede bir)
        if idx % 25 == 0:
            save_checkpoint(args.checkpoint_file, processed_ids)

        if args.delay > 0:
            time.sleep(args.delay)

    # Son durum kaydı
    save_checkpoint(args.checkpoint_file, processed_ids)
    elapsed = time.time() - start_time
    logger.info("=" * 60)
    logger.info(f"🏁 Çeviri tamamlandı! Başarılı: {success_count}, Başarısız: {fail_count}, Süre: {int(elapsed)}sn")
    logger.info(f"💾 Güncel checkpoint kaydedildi: {len(processed_ids)} kayıt.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
