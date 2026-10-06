"""
src/anilist_client.py — AniList GraphQL API Client

AniList API üzerinden anime verilerini çeker:
    - id           → AniList benzersiz anime kimliği
    - idMal        → MyAnimeList anime kimliği
    - bannerImage  → Geniş format kapak görseli URL'si
    - poster_url   → coverImage.extraLarge / large poster görseli URL'si
    - airing / releasing / season anime listeleri

Rate Limit:
    - AniList API: 90 istek/dakika
    - İstekler arası minimum 0.8 saniye bekleme
    - HTTP 429 durumunda Retry-After header'ı kontrol edilir
    - Üstel geri çekilme (exponential backoff) uygulanır
"""

import time
import requests
from typing import Optional

try:
    from src.logger import setup_logger
except ImportError:
    from logger import setup_logger


class AnilistClient:
    """AniList GraphQL API ile anime verilerini çeken client."""

    API_URL = "https://graphql.anilist.co"

    RATE_LIMIT_DELAY = 0.8
    RETRY_MAX = 3
    RETRY_BACKOFF = 10
    REQUEST_TIMEOUT = 15
    BATCH_SIZE = 50

    SINGLE_QUERY = """
    query ($malId: Int) {
      Media(idMal: $malId, type: ANIME) {
        id
        idMal
        title {
          romaji
          english
        }
        status
        episodes
        bannerImage
        coverImage {
          extraLarge
          large
          medium
        }
      }
    }
    """

    RELEASING_QUERY = """
    query ($page: Int, $perPage: Int) {
      Page(page: $page, perPage: $perPage) {
        pageInfo {
          hasNextPage
          currentPage
          total
        }
        media(type: ANIME, status_in: [RELEASING], sort: [POPULARITY_DESC]) {
          id
          idMal
          title {
            romaji
            english
          }
          status
          episodes
          nextAiringEpisode {
            episode
            timeUntilAiring
          }
          bannerImage
          coverImage {
            extraLarge
            large
            medium
          }
          season
          seasonYear
        }
      }
    }
    """

    RECENTLY_UPDATED_QUERY = """
    query ($page: Int, $perPage: Int) {
      Page(page: $page, perPage: $perPage) {
        pageInfo {
          hasNextPage
          currentPage
        }
        media(type: ANIME, sort: [UPDATED_AT_DESC]) {
          id
          idMal
          title {
            romaji
            english
          }
          status
          episodes
          nextAiringEpisode {
            episode
            timeUntilAiring
          }
          bannerImage
          coverImage {
            extraLarge
            large
            medium
          }
          season
          seasonYear
        }
      }
    }
    """

    SEASON_QUERY = """
    query ($season: MediaSeason, $seasonYear: Int, $page: Int, $perPage: Int) {
      Page(page: $page, perPage: $perPage) {
        pageInfo {
          hasNextPage
          currentPage
        }
        media(type: ANIME, season: $season, seasonYear: $seasonYear, sort: [POPULARITY_DESC]) {
          id
          idMal
          title {
            romaji
            english
          }
          status
          episodes
          bannerImage
          coverImage {
            extraLarge
            large
            medium
          }
          season
          seasonYear
        }
      }
    }
    """

    def __init__(self):
        self.logger = setup_logger("Anilist")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AnimoriDepo/2.0 (GitHub Actions)",
        })
        self._last_request_time = 0

    def _wait_rate_limit(self):
        """İstekler arası minimum bekleme süresini uygular."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)

    def _request_with_retry(self, payload: dict) -> Optional[dict]:
        """GraphQL isteğini retry ve backoff mekanizmasıyla gönderir."""
        for attempt in range(self.RETRY_MAX):
            self._wait_rate_limit()

            try:
                self._last_request_time = time.time()
                response = self.session.post(
                    self.API_URL,
                    json=payload,
                    timeout=self.REQUEST_TIMEOUT,
                )

                if response.status_code == 200:
                    return response.json()

                if response.status_code == 404:
                    try:
                        body = response.json()
                        if "data" in body:
                            return body
                    except (ValueError, KeyError):
                        pass
                    return None

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = int(retry_after) + 1 if retry_after else self.RETRY_BACKOFF * (attempt + 1)
                    self.logger.warning(
                        f"Rate limit aşıldı (429). {wait_time}s bekleniyor... (deneme {attempt + 1}/{self.RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                if response.status_code in (500, 502, 503):
                    wait_time = self.RETRY_BACKOFF * (attempt + 1)
                    self.logger.warning(
                        f"Sunucu hatası ({response.status_code}). {wait_time}s bekleniyor... (deneme {attempt + 1}/{self.RETRY_MAX})"
                    )
                    time.sleep(wait_time)
                    continue

                self.logger.error(
                    f"AniList API hatası: HTTP {response.status_code} — {response.text[:200]}"
                )
                return None

            except requests.exceptions.Timeout:
                wait_time = self.RETRY_BACKOFF * (attempt + 1)
                self.logger.warning(
                    f"İstek zaman aşımı. {wait_time}s bekleniyor... (deneme {attempt + 1}/{self.RETRY_MAX})"
                )
                time.sleep(wait_time)
                continue

            except requests.exceptions.RequestException as e:
                self.logger.error(f"AniList bağlantı hatası: {e}")
                return None

        self.logger.error(f"AniList API {self.RETRY_MAX} denemede de başarısız oldu.")
        return None

    @staticmethod
    def _parse_media(media: Optional[dict]) -> Optional[dict]:
        """AniList Media yanıtını iç formata dönüştürür."""
        if not media:
            return None

        cover = media.get("coverImage") or {}
        poster_url = cover.get("extraLarge") or cover.get("large") or cover.get("medium")

        return {
            "id": media.get("id"),
            "idMal": media.get("idMal"),
            "banner_image": media.get("bannerImage"),
            "poster_url": poster_url,
            "title_romaji": (media.get("title") or {}).get("romaji"),
            "title_english": (media.get("title") or {}).get("english"),
            "status": media.get("status"),
            "episodes": media.get("episodes"),
        }

    def fetch(self, mal_id: int) -> Optional[dict]:
        """Tek bir anime için AniList verisini çeker (mal_id ile)."""
        payload = {
            "query": self.SINGLE_QUERY,
            "variables": {"malId": mal_id},
        }

        result = self._request_with_retry(payload)
        if not result:
            return None

        if "errors" in result:
            return None

        media = result.get("data", {}).get("Media")
        return self._parse_media(media)

    def fetch_releasing(self, page: int = 1, per_page: int = 50) -> tuple[list[dict], bool]:
        """
        Yayında olan (RELEASING) animeleri çeker.
        Returns: (media_list, has_next_page)
        """
        payload = {
            "query": self.RELEASING_QUERY,
            "variables": {"page": page, "perPage": per_page},
        }
        res = self._request_with_retry(payload)
        if not res or "data" not in res:
            return [], False

        page_data = res["data"].get("Page", {})
        media_list = page_data.get("media", [])
        has_next = page_data.get("pageInfo", {}).get("hasNextPage", False)

        parsed_list = []
        for m in media_list:
            parsed = self._parse_media(m)
            if parsed:
                parsed_list.append(parsed)

        return parsed_list, has_next

    def fetch_recently_updated(self, page: int = 1, per_page: int = 50) -> tuple[list[dict], bool]:
        """
        Son güncellenen animeleri çeker.
        Returns: (media_list, has_next_page)
        """
        payload = {
            "query": self.RECENTLY_UPDATED_QUERY,
            "variables": {"page": page, "perPage": per_page},
        }
        res = self._request_with_retry(payload)
        if not res or "data" not in res:
            return [], False

        page_data = res["data"].get("Page", {})
        media_list = page_data.get("media", [])
        has_next = page_data.get("pageInfo", {}).get("hasNextPage", False)

        parsed_list = []
        for m in media_list:
            parsed = self._parse_media(m)
            if parsed:
                parsed_list.append(parsed)

        return parsed_list, has_next

    def fetch_season(self, season: str, season_year: int, page: int = 1, per_page: int = 50) -> tuple[list[dict], bool]:
        """
        Belirli bir sezondaki animeleri çeker (season: WINTER, SPRING, SUMMER, FALL).
        Returns: (media_list, has_next_page)
        """
        payload = {
            "query": self.SEASON_QUERY,
            "variables": {
                "season": season.upper(),
                "seasonYear": season_year,
                "page": page,
                "perPage": per_page,
            },
        }
        res = self._request_with_retry(payload)
        if not res or "data" not in res:
            return [], False

        page_data = res["data"].get("Page", {})
        media_list = page_data.get("media", [])
        has_next = page_data.get("pageInfo", {}).get("hasNextPage", False)

        parsed_list = []
        for m in media_list:
            parsed = self._parse_media(m)
            if parsed:
                parsed_list.append(parsed)

        return parsed_list, has_next
