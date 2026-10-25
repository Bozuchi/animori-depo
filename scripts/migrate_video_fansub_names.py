"""
scripts/migrate_video_fansub_names.py — Video kayıtlarındaki fansub_id'yi fansub adına çevirir

Eski yapıda turkanime.episodes[].videos[] kayıtları fansub'u sayısal id ile tutuyordu:

    {"fansub_id": 46, "player": "GDRIVE", "url": "..."}

Yeni yapıda id yerine doğrudan fansub adı yazılır (api/metadata.json → fansubs):

    {"fansub": "AnimeOU", "player": "GDRIVE", "url": "..."}

Script idempotenttir: fansub_id'si olmayan (zaten dönüştürülmüş) videolara dokunmaz.
Eşleşmeyen fansub_id olursa dosya yine de yazılır, ancak id raporda listelenir.

Kullanım:
    python scripts/migrate_video_fansub_names.py            # uygula
    python scripts/migrate_video_fansub_names.py --dry-run  # sadece raporla
"""

import os
import sys
import json

# Proje kök dizinini Python path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.storage_manager import StorageManager
from src.logger import setup_logger

logger = setup_logger("MigrateFansubNames")

METADATA_PATH = os.path.join("api", "metadata.json")


def load_fansubs() -> dict[str, str]:
    """metadata.json içindeki fansubs sözlüğünü {id_str: ad} olarak döner."""
    with open(METADATA_PATH, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    fansubs = {}
    for fid, info in (metadata.get("fansubs") or {}).items():
        if isinstance(info, dict):
            fansubs[str(fid)] = info.get("name")
        else:
            fansubs[str(fid)] = str(info)
    return fansubs


def run(dry_run: bool = False):
    fansubs = load_fansubs()
    logger.info(f"📚 metadata.json fansub sayısı: {len(fansubs)}")

    storage = StorageManager()
    anime_dir = storage.anime_dir

    files = sorted(f for f in os.listdir(anime_dir) if f.endswith(".json"))
    logger.info(f"📂 {len(files)} anime dosyası taranıyor...")

    changed_files = 0
    migrated = 0
    already = 0
    no_fansub = 0
    unknown_ids = {}
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

        file_changed = False
        episodes = (data.get("turkanime") or {}).get("episodes") or []

        for ep in episodes:
            videos = ep.get("videos") or []
            for idx, video in enumerate(videos):
                if not isinstance(video, dict):
                    continue

                if "fansub_id" in video:
                    fid = str(video["fansub_id"])
                    name = fansubs.get(fid)
                    if name is None:
                        unknown_ids[fid] = unknown_ids.get(fid, 0) + 1
                        name = f"Fansub {fid}"

                    # fansub adı başa gelecek şekilde yeniden kur
                    new_video = {"fansub": name}
                    for key, value in video.items():
                        if key in ("fansub_id", "fansub"):
                            continue
                        new_video[key] = value
                    videos[idx] = new_video

                    migrated += 1
                    file_changed = True
                elif "fansub" in video:
                    already += 1
                else:
                    no_fansub += 1

        if file_changed:
            changed_files += 1
            if not dry_run:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    logger.error(f"Dosya yazılamadı ({fname}): {e}")
                    skipped += 1

        if (i + 1) % 1000 == 0:
            logger.info(f"⏳ İlerleme: {i + 1}/{len(files)}")

    logger.info("==========================================")
    logger.info(f"{'🧪 [DRY-RUN] ' if dry_run else ''}Değişen dosya: {changed_files}")
    logger.info(f"fansub_id → fansub: {migrated} video")
    logger.info(f"Zaten fansub adı olan: {already} | fansub bilgisi olmayan: {no_fansub}")
    logger.info(f"Atlanan dosya: {skipped}")
    if unknown_ids:
        logger.warning(f"metadata'da bulunamayan fansub_id: {unknown_ids}")
    else:
        logger.info("metadata'da bulunamayan fansub_id: yok")
    logger.info("==========================================")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
