"""
storage_manager.py — Dosya ve Klasör Yöneticisi

Çekilen anime verilerini statik JSON dosyaları olarak organize eder:
    - api/anime/{mal_id}.json  → Her anime için detaylı veri (Jikan'da yoksa {slug}.json)
    - api/animes.json        → Hafifletilmiş indeks (vitrin için)
    - api/metadata.json      → Ortak metadata (türler, stüdyolar ve fansublar)
    - api/slug_map.json      → Hızlı arama için slug -> dosya adı haritası
    - api/version.json       → Son güncelleme bilgisi
"""

import os
import re
import json
import hashlib
from datetime import datetime
try:
    from src.logger import setup_logger
except ImportError:
    from logger import setup_logger


class StorageManager:
    """Statik JSON API dosyalarını yöneten sınıf."""

    def __init__(self, base_dir: str = "api"):
        self.base_dir = base_dir
        self.anime_dir = os.path.join(base_dir, "anime")
        self.slug_map_path = os.path.join(base_dir, "slug_map.json")
        self.metadata_path = os.path.join(base_dir, "metadata.json")
        self.logger = setup_logger("Storage")

        # Klasörleri oluştur
        os.makedirs(self.anime_dir, exist_ok=True)
        
        # Hızlı arama için slug -> dosya_adi haritası
        self.slug_to_file = {}
        self._load_slug_map()
        self.metadata = self._load_metadata()

    def _load_metadata(self) -> dict:
        """metadata.json dosyasını yükler."""
        if os.path.exists(self.metadata_path):
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "genres" not in data:
                        data["genres"] = {}
                    if "studios" not in data:
                        data["studios"] = {}
                    if "fansubs" not in data:
                        data["fansubs"] = {}
                    return data
            except Exception as e:
                self.logger.warning(f"metadata.json okunamadı: {e}")
        return {"genres": {}, "studios": {}, "fansubs": {}}

    def save_metadata(self):
        """metadata.json dosyasını diske kaydeder."""
        try:
            with open(self.metadata_path, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, ensure_ascii=False, indent=4)
            self.logger.info("💾 metadata.json güncellendi.")
        except Exception as e:
            self.logger.error(f"metadata.json kaydedilemedi: {e}")

    def _load_slug_map(self):
        """
        slug -> filename eşleşmesini yükler.
        Önce slug_map.json'dan okumayı dener (hızlı).
        Dosya yoksa api/anime/*.json'ları tarayarak yeniden oluşturur.
        """
        # Önce slug_map.json'dan yüklemeyi dene (hızlı)
        if os.path.exists(self.slug_map_path):
            try:
                with open(self.slug_map_path, "r", encoding="utf-8") as f:
                    self.slug_to_file = json.load(f)
                self.logger.info(f"✅ slug_map.json yüklendi. ({len(self.slug_to_file)} anime)")
                return
            except Exception as e:
                self.logger.warning(f"slug_map.json parse hatası, tam taramaya geçiliyor: {e}")

        # slug_map.json yoksa veya bozuksa, tüm dosyaları tarayarak oluştur
        self.logger.info("⏳ slug_map.json bulunamadı, anime dosyaları taranıyor...")
        if not os.path.exists(self.anime_dir):
            return

        for filename in os.listdir(self.anime_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(self.anime_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        slug = data.get("turkanime", {}).get("slug") or data.get("slug")
                        if slug:
                            self.slug_to_file[slug] = filename
                except Exception as e:
                    self.logger.warning(f"Anime dosyası okunamadı ({filename}): {e}")
                    continue

        # Tarama sonucunu kaydet (bir sonraki çalıştırmada hızlı yüklensin)
        self._save_slug_map()
        self.logger.info(f"✅ slug_map.json oluşturuldu. ({len(self.slug_to_file)} anime)")

    def _save_slug_map(self):
        """slug_to_file haritasını slug_map.json'a kaydeder."""
        try:
            with open(self.slug_map_path, "w", encoding="utf-8") as f:
                json.dump(self.slug_to_file, f, ensure_ascii=False)
        except Exception as e:
            self.logger.warning(f"slug_map.json kaydedilemedi: {e}")

    def save_slug_map(self):
        """slug_to_file haritasını diske kaydeder (dış kullanım için)."""
        self._save_slug_map()

    def save_anime_detail(
        self,
        slug: str,
        turkanime_data: dict | None = None,
        jikan_data: dict | None = None,
        anilist_data: dict | None = None,
        synopsis_tr: str | None = None,
        mal_id: int | None = None,
        tmdb_data: dict | None = None,
    ):
        """
        Tek bir anime için birleştirilmiş detay dosyası oluşturur veya günceller.

        Args:
            slug: Anime slug'ı.
            turkanime_data: Türkanime'den gelen veriler (None olabilir).
            jikan_data: MAL/Jikan verileri (veya None).
            anilist_data: AniList verileri (veya None).
            synopsis_tr: Türkçe özet (veya None).
            mal_id: MAL ID (veya None).
            tmdb_data: TMDB eşleme verileri (id, type, season).
        """
        mid = mal_id or (jikan_data.get("mal_id") if jikan_data else None)
        file_basename = str(mid) if mid else slug
        filename = f"{file_basename}.json"
        filepath = os.path.join(self.anime_dir, filename)

        # Mevcut veriyi oku
        existing = self.load_anime_detail(file_basename) or self.load_anime_detail(slug) or {}

        # turkanime objesi
        if turkanime_data:
            turkanime_obj = {**turkanime_data, "slug": slug}
        elif "turkanime" in existing and existing["turkanime"]:
            turkanime_obj = existing["turkanime"]
        else:
            title_name = (jikan_data.get("title") if jikan_data else "") or slug
            turkanime_obj = {"isim": title_name, "slug": slug}

        detail = {
            "turkanime": turkanime_obj,
            "jikan": jikan_data if jikan_data is not None else existing.get("jikan"),
            "anilist": anilist_data if anilist_data is not None else existing.get("anilist"),
        }

        # tmdb bilgisi
        if tmdb_data is not None:
            detail["tmdb"] = tmdb_data
        elif "tmdb" in existing:
            detail["tmdb"] = existing["tmdb"]

        # synopsis_tr ekle veya koru
        if synopsis_tr:
            detail["synopsis_tr"] = synopsis_tr
        elif "synopsis_tr" in existing:
            detail["synopsis_tr"] = existing["synopsis_tr"]

        # Mevcut dosyadaki episodes ve diğer ek alanları koru
        for key, value in existing.items():
            if key not in detail:
                detail[key] = value

        # Eski birleşik üst seviye bölüm listesi artık kullanılmıyor:
        # Türkanime bölümleri → turkanime.episodes, MAL bölümleri → jikan.episode_list
        detail.pop("episodes", None)

        if "anilist" not in detail:
            detail["anilist"] = None

        # year ve season bilgisi yoksa aired.from üzerinden tamamla
        jikan_obj = detail.get("jikan")
        if jikan_obj:
            # 0 sentinel'i tek bir "bilinmiyor" değerine (null) normalize edilir
            if jikan_obj.get("episodes") == 0:
                jikan_obj["episodes"] = None

            aired_obj = jikan_obj.get("aired")
            aired_from = str(aired_obj.get("from", "")).strip() if isinstance(aired_obj, dict) and aired_obj.get("from") else None
            if aired_from:
                if jikan_obj.get("year") is None:
                    m_year = re.match(r"^(\d{4})", aired_from)
                    if m_year:
                        try:
                            jikan_obj["year"] = int(m_year.group(1))
                        except ValueError:
                            pass
                if not jikan_obj.get("season"):
                    m_month = re.match(r"^\d{4}-(\d{2})", aired_from)
                    if m_month:
                        try:
                            month = int(m_month.group(1))
                            if month in (1, 2, 3):
                                jikan_obj["season"] = "winter"
                            elif month in (4, 5, 6):
                                jikan_obj["season"] = "spring"
                            elif month in (7, 8, 9):
                                jikan_obj["season"] = "summer"
                            elif month in (10, 11, 12):
                                jikan_obj["season"] = "fall"
                        except ValueError:
                            pass

        # Eğer önceden başka bir isimle (örneğin slug.json) kaydedildiyse ve
        # şimdi isim mal_id.json olarak değiştiyse, eskisini sil
        old_filename = self.slug_to_file.get(slug)
        if old_filename and old_filename != filename:
            old_filepath = os.path.join(self.anime_dir, old_filename)
            if os.path.exists(old_filepath):
                try:
                    os.remove(old_filepath)
                except OSError:
                    pass

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(detail, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"Anime detay dosyası kaydedilemedi ({file_basename}): {e}")
            return

        self.slug_to_file[slug] = filename
        if mid:
            self.slug_to_file[str(mid)] = filename

    def load_anime_detail(self, identifier: str | int) -> dict | None:
        """
        Mevcut bir anime detay dosyasını okur (mal_id veya slug ile).

        Args:
            identifier: Anime mal_id'si veya slug'ı.

        Returns:
            dict: Anime verisi veya None (dosya yoksa).
        """
        if not identifier:
            return None

        id_str = str(identifier)
        direct_path = os.path.join(self.anime_dir, f"{id_str}.json")
        if os.path.exists(direct_path):
            try:
                with open(direct_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Dosya okunamadı: {direct_path} — {e}")

        filename = self.slug_to_file.get(id_str)
        if not filename:
            return None

        filepath = os.path.join(self.anime_dir, filename)
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            self.logger.warning(f"Dosya okunamadı: {filepath} — {e}")
            return None

    def update_anime_tmdb(self, identifier: str | int, tmdb_info: dict | None) -> bool:
        """
        Belirtilen anime dosyasındaki sadece 'tmdb' alanını günceller.
        Diğer tüm alanlar (episodes, jikan, anilist vb.) korunur.

        Args:
            identifier: mal_id veya slug.
            tmdb_info: {"id": int, "type": "tv"|"movie", "season": int} veya None.

        Returns:
            bool: Güncelleme başarılı mı / dosya yazıldı mı?
        """
        existing = self.load_anime_detail(identifier)
        if not existing:
            return False

        # Mevcut başlık adı (varsa) korunur: yeni eşleşmede ad üretilememişse
        # var olan isim silinmez (AnimeciX kataloğundan gelen isimler de korunur).
        if isinstance(tmdb_info, dict) and not tmdb_info.get("name"):
            old_tmdb = existing.get("tmdb")
            if isinstance(old_tmdb, dict) and old_tmdb.get("name"):
                tmdb_info = {**tmdb_info, "name": old_tmdb["name"]}

        if existing.get("tmdb") == tmdb_info:
            return True  # Zaten güncel, disk yazımı gereksiz

        existing["tmdb"] = tmdb_info

        id_str = str(identifier)
        filename = f"{id_str}.json"
        if not os.path.exists(os.path.join(self.anime_dir, filename)):
            filename = self.slug_to_file.get(id_str, filename)

        filepath = os.path.join(self.anime_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self.logger.error(f"Anime TMDB kaydedilemedi ({identifier}): {e}")
            return False

    @staticmethod
    def _split_legacy_episodes(legacy_eps: list[dict]) -> tuple[list[dict], list[dict]]:
        """
        Eski birleşik üst seviye 'episodes' dizisini ikiye ayırır.

        Returns:
            tuple: (turkanime_episodes, jikan_episode_list)
                - turkanime_episodes: Türkanime kaynaklı bölümler (title, videos, skip_times, added_date)
                - jikan_episode_list: MyAnimeList kaynaklı bölümler (title, aired, filler, recap)
        """
        turkanime_eps = []
        jikan_eps = []

        for ep in legacy_eps or []:
            num = ep.get("episode_number")

            # ── Türkanime tarafı: gerçek Türkanime verisi olan kayıtlar ──
            # Film/OVA gibi tek bölümlük yapıtlarda episode_number null olabilir;
            # bu kayıtlar video linklerini taşıdığı için korunur.
            tk_title = ep.get("turkanime_title")
            placeholder_title = f"{num}. Bölüm"
            has_tk_data = (
                bool(ep.get("videos"))
                or bool(ep.get("skip_times"))
                or (bool(tk_title) and tk_title != placeholder_title)
            )
            if has_tk_data:
                tk_ep = {
                    "episode_number": num,
                    "title": tk_title or ep.get("title") or (placeholder_title if num is not None else None),
                    "videos": ep.get("videos") or [],
                }
                if ep.get("skip_times"):
                    tk_ep["skip_times"] = ep["skip_times"]
                if ep.get("added_date"):
                    tk_ep["added_date"] = ep["added_date"]
                turkanime_eps.append(tk_ep)

            # ── MAL tarafı: sadece numaralı bölümler ──
            if num is None:
                continue

            has_mal_data = (
                ep.get("jikan_mal_id") is not None
                or bool(ep.get("aired"))
                or bool(ep.get("title"))
            )
            if has_mal_data:
                jikan_eps.append({
                    "episode_number": num,
                    "title": ep.get("title") or placeholder_title,
                    "aired": ep.get("aired"),
                    "filler": ep.get("filler", False),
                    "recap": ep.get("recap", False),
                })

        return turkanime_eps, jikan_eps

    def merge_and_save_episodes(
        self,
        mal_id: int,
        episodes_input: dict[int, dict] | list[dict],
        slug: str | None = None
    ) -> int:
        """
        MAL'dan gelen bölüm listesini iki ayrı alanda saklar:
        - jikan.episode_list : doğrudan MAL'den gelen bölüm listesi (title, aired, filler, recap)
        - turkanime.episodes : Türkanime kaynaklı bölümler (title, videos, skip_times, added_date)

        Eski birleşik üst seviye 'episodes' dizisi kaldırılır. Türkanime'de olup MAL
        listesinde bulunmayan bölümler turkanime.episodes içinde korunur; MAL'de olup
        Türkanime'de karşılığı olmayan bölümler ise turkanime.episodes'a eklenmez.
        Böylece jikan.episode_list her zaman sadece MAL verisini içerir.

        Args:
            mal_id: MyAnimeList anime ID'si.
            episodes_input: MAL'dan çekilen bölümler.
            slug: Varsa slug.

        Returns:
            int: MAL listesine yeni eklenen bölüm sayısı.
        """
        existing = self.load_anime_detail(mal_id) or (self.load_anime_detail(slug) if slug else None)
        if not existing:
            self.logger.warning(f"Anime dosyası bulunamadı, bölümler birleştirilemedi: {mal_id}")
            return 0

        if isinstance(episodes_input, dict):
            new_list = list(episodes_input.values())
        else:
            new_list = episodes_input

        new_by_num = {e.get("episode_number"): e for e in new_list if e.get("episode_number") is not None}

        jikan = existing.get("jikan") or {}
        turkanime = existing.get("turkanime") or {}

        # Mevcut yeni yapı; yoksa eski birleşik diziden türet
        old_jikan_eps = jikan.get("episode_list") or []
        old_turkanime_eps = turkanime.get("episodes") or []
        if not old_jikan_eps and not old_turkanime_eps and existing.get("episodes"):
            old_turkanime_eps, old_jikan_eps = self._split_legacy_episodes(existing["episodes"])

        # MAL listesi boş geldiyse (geçici hata / rate limit) mevcut listeyi ezme
        if not new_by_num and old_jikan_eps:
            self.logger.warning(f"MAL bölüm listesi boş geldi, mevcut liste korunuyor: {mal_id}")
            return 0

        old_jikan_nums = {e.get("episode_number") for e in old_jikan_eps if e.get("episode_number") is not None}
        old_tk_by_num = {e.get("episode_number"): e for e in old_turkanime_eps if e.get("episode_number") is not None}
        # Film/OVA gibi episode_number'ı olmayan Türkanime kayıtları aynen korunur
        old_tk_unnumbered = [e for e in old_turkanime_eps if e.get("episode_number") is None]

        new_added_count = 0

        # ── MAL listesi (jikan.episode_list) ──
        jikan_episode_list = []
        for num in sorted(new_by_num):
            info = new_by_num[num]
            jikan_episode_list.append({
                "episode_number": num,
                "title": info.get("title") or f"Bölüm {num}",
                "aired": info.get("aired"),
                "filler": info.get("filler", False),
                "recap": info.get("recap", False),
            })
            if num not in old_jikan_nums:
                new_added_count += 1

        # ── Türkanime bölümleri: mevcut kayıtlar aynen korunur ──
        # (MAL'de olup Türkanime'de karşılığı olmayanlar buraya eklenmez)
        turkanime_eps = [old_tk_by_num[num] for num in sorted(old_tk_by_num)] + old_tk_unnumbered

        jikan["episode_list"] = jikan_episode_list
        turkanime["episodes"] = turkanime_eps
        existing["jikan"] = jikan
        existing["turkanime"] = turkanime
        existing.pop("episodes", None)

        filename = f"{mal_id}.json"
        filepath = os.path.join(self.anime_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"Episodes kaydedilemedi ({mal_id}): {e}")

        return new_added_count

    def save_episodes(self, slug: str, episodes: list[dict], mal_id: int | None = None):
        """
        Mevcut anime JSON dosyasına 'episodes' dizisini doğrudan ekler/günceller.
        (Geriye dönük uyumluluk için).
        """
        mid = mal_id or (int(slug) if slug.isdigit() else None)
        if mid:
            return self.merge_and_save_episodes(mid, episodes, slug=slug)

        existing = self.load_anime_detail(slug)
        if existing is None:
            self.logger.warning(f"Episodes kaydedilemedi, anime dosyası bulunamadı: {slug}")
            return

        existing["episodes"] = episodes
        filename = self.slug_to_file.get(slug, f"{slug}.json")
        filepath = os.path.join(self.anime_dir, filename)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"Episodes kaydedilemedi ({slug}): {e}")

    def build_index(self) -> int:
        """
        Tüm anime detay dosyalarını tarayarak kapsamlı indeks oluşturur.

        animes.json içeriği:
            [
                {
                    "mal_id": 1735,
                    "title": "Naruto: Shippuuden",
                    "title_english": "Naruto: Shippuden",
                    "slug": "naruto-shippuuden",
                    "image_url": "https://cdn.myanimelist.net/...",
                    "type": "TV",
                    "status": "Finished Airing",
                    "year": 2007,
                    "season": "spring",
                    "score": 8.25,
                    "popularity": 8,
                    "genres": [1, 27],
                    "themes": [17],
                    "demographics": [27],
                    "studios": [37]
                },
                ...
            ]

        Returns:
            int: Toplam anime sayısı.
        """
        index = []

        for filename in sorted(os.listdir(self.anime_dir)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(self.anime_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                continue

            turkanime = data.get("turkanime", {})
            jikan = data.get("jikan") or {}
            anilist = data.get("anilist") or {}
            
            slug_val = turkanime.get("slug") or data.get("slug")
            
            # mal_id'yi belirle (jikan'da yoksa fallback olarak slug kullan)
            mal_id_val = jikan.get("mal_id") if jikan.get("mal_id") else None
            
            # İlişkili listeleri filtrele (zaten jikan'da integer array olarak geliyorlar)
            genres = jikan.get("genres", [])
            themes = jikan.get("themes", [])
            demographics = jikan.get("demographics", [])
            studios = jikan.get("studios", [])

            # year ve season belirleme: jikan verisinde yoksa aired.from üzerinden çıkar
            year_val = jikan.get("year")
            season_val = jikan.get("season")
            aired_data = jikan.get("aired")
            aired_from = str(aired_data.get("from", "")).strip() if isinstance(aired_data, dict) and aired_data.get("from") else None

            if aired_from:
                if year_val is None:
                    m_year = re.match(r"^(\d{4})", aired_from)
                    if m_year:
                        try:
                            year_val = int(m_year.group(1))
                        except ValueError:
                            year_val = None
                if not season_val:
                    m_month = re.match(r"^\d{4}-(\d{2})", aired_from)
                    if m_month:
                        try:
                            month = int(m_month.group(1))
                            if month in (1, 2, 3):
                                season_val = "winter"
                            elif month in (4, 5, 6):
                                season_val = "spring"
                            elif month in (7, 8, 9):
                                season_val = "summer"
                            elif month in (10, 11, 12):
                                season_val = "fall"
                        except ValueError:
                            season_val = None

            # Toplam bölüm: jikan.episodes bilinmiyorsa (null/0) MAL bölüm listesinin uzunluğu kullanılır
            episodes_val = jikan.get("episodes") or len(jikan.get("episode_list") or []) or None

            index.append({
                "mal_id": mal_id_val,
                "anilist_id": anilist.get("id"),
                "title": jikan.get("title") or turkanime.get("isim", ""),
                "title_english": jikan.get("title_english"),
                "slug": slug_val,
                "image_url": anilist.get("poster_url") or jikan.get("image_url"),
                "type": jikan.get("type"),
                "status": jikan.get("status"),
                "episodes": episodes_val,
                "year": year_val,
                "season": season_val,
                "score": jikan.get("score"),
                "popularity": jikan.get("popularity"),
                "genres": genres,
                "themes": themes,
                "demographics": demographics,
                "studios": studios
            })

        # İndeks dosyasını yaz
        index_path = os.path.join(self.base_dir, "animes.json")
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"animes.json kaydedilemedi: {e}")
            return 0

        self.logger.info(f"✅ animes.json oluşturuldu. ({len(index)} anime)")
        return len(index)

    @staticmethod
    def _compute_file_hash(filepath: str) -> str | None:
        """
        Dosyanın içeriğinin MD5 hash'inin ilk 8 karakterini döndürür.
        Değişiklik tespiti amaçlıdır, kriptografik güvenlik gerektirmez.

        Args:
            filepath: Hash'i hesaplanacak dosyanın yolu.

        Returns:
            str: 8 karakterlik hash veya None (dosya yoksa).
        """
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except (IOError, OSError):
            return None

    def update_versions(self):
        """
        version.json dosyasını hash tabanlı değişiklik tespiti ile günceller.

        Takip edilen dosyalar:
            - animes.json     → İndeks
            - metadata.json   → Ortak metadata (türler, stüdyolar, fansublar)

        Mantık:
            - Her dosyanın MD5 hash'i hesaplanır (ilk 8 karakter)
            - Hash değişmişse → last_updated güncellenir
            - Hash aynıysa → eski last_updated korunur
            - Sonuç version.json'a yazılır

        Yapı:
            {
                "animes": {
                    "last_updated": "20260719_0337",
                    "hash": "a1b2c3d4"
                },
                "metadata": {
                    "last_updated": "20260719_0337",
                    "hash": "e5f6g7h8"
                }
            }
        """
        version_path = os.path.join(self.base_dir, "version.json")
        now_str = datetime.now().strftime("%Y%m%d_%H%M")

        # Mevcut version.json'ı oku (varsa)
        old_version = {}
        if os.path.exists(version_path):
            try:
                with open(version_path, "r", encoding="utf-8") as f:
                    old_version = json.load(f)
            except (json.JSONDecodeError, IOError):
                old_version = {}

        # Takip edilecek dosyalar ve ek alanları
        tracked_files = {
            "animes": os.path.join(self.base_dir, "animes.json"),
            "metadata": self.metadata_path,
        }

        new_version = {}
        changed_keys = []

        for key, filepath in tracked_files.items():
            new_hash = self._compute_file_hash(filepath)
            old_entry = old_version.get(key, {})
            old_hash = old_entry.get("hash")

            if new_hash is None:
                # Dosya henüz oluşmamış → atla
                continue

            if new_hash != old_hash:
                # İçerik değişmiş → last_updated güncelle
                entry = {"last_updated": now_str, "hash": new_hash}
                changed_keys.append(key)
            else:
                # İçerik aynı → eski last_updated'i koru
                entry = {"last_updated": old_entry.get("last_updated", now_str), "hash": old_hash}

            new_version[key] = entry

        # Diske yaz
        try:
            with open(version_path, "w", encoding="utf-8") as f:
                json.dump(new_version, f, ensure_ascii=False, indent=2)
        except (IOError, OSError) as e:
            self.logger.error(f"version.json kaydedilemedi: {e}")
            return

        if changed_keys:
            self.logger.info(f"✅ version.json güncellendi. (değişen: {', '.join(changed_keys)})")
        else:
            self.logger.info("ℹ️  Hiçbir dosya değişmedi, version.json korundu.")
