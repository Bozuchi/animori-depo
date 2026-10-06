"""
src/translator.py — Anime Özeti Türkçe Çeviri Motoru

Google Translate (Chrome Extension API) ve deep-translator (MyMemory fallback)
kullanarak MyAnimeList veya AniList üzerinden alınan İngilizce özetleri
yüksek güvenilirlikle otomatik olarak Türkçeye çevirir.
"""

import time
import requests
from typing import Optional
from src.logger import setup_logger

logger = setup_logger("Translator")

try:
    from deep_translator import MyMemoryTranslator
    _MYMEMORY_AVAILABLE = True
except ImportError:
    _MYMEMORY_AVAILABLE = False


class SynopsisTranslator:
    """Anime özetlerini Türkçeye çeviren hibrit servis."""

    GOOGLE_CHROME_API = "https://clients5.google.com/translate_a/t"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    MAX_RETRIES = 3
    RETRY_DELAY = 1.5

    def __init__(self, target_lang: str = "tr"):
        self.target_lang = target_lang
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.USER_AGENT})
        self._fallback_translator = None
        if _MYMEMORY_AVAILABLE:
            try:
                self._fallback_translator = MyMemoryTranslator(source="en-US", target="tr-TR")
            except Exception as e:
                self.logger.warning(f"Fallback çevirici başlatılamadı: {e}")

    def _translate_google(self, text: str) -> Optional[str]:
        """Google Translate Chrome Extension API ile hızlı ve kesintisiz çeviri."""
        params = {
            "client": "dict-chrome-ex",
            "sl": "auto",
            "tl": self.target_lang,
            "q": text,
        }
        response = self.session.get(self.GOOGLE_CHROME_API, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                first = data[0]
                if isinstance(first, list) and len(first) > 0 and isinstance(first[0], str):
                    return first[0].strip()
                elif isinstance(first, str):
                    return first.strip()
        return None

    def _translate_fallback(self, text: str) -> Optional[str]:
        """İkincil çevirici (MyMemory)."""
        if self._fallback_translator:
            try:
                # MyMemory için 500 karakter sınırları olduğundan parça başı çeviri
                return self._fallback_translator.translate(text[:500])
            except Exception as e:
                self.logger.warning(f"Fallback çeviri başarısız: {e}")
        return None

    def translate(self, text: Optional[str]) -> Optional[str]:
        """
        Verilen metni Türkçeye çevirir.
        
        Args:
            text: Çevrilecek İngilizce özet metni.
            
        Returns:
            str: Türkçe çeviri veya hata/boşluk durumunda None.
        """
        if not text or not text.strip():
            return None

        clean_text = text.strip()

        # Uzun metinler için güvenli kırpma
        if len(clean_text) > 4000:
            clean_text = clean_text[:4000]

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = self._translate_google(clean_text)
                if result:
                    return result
            except Exception as e:
                self.logger.warning(
                    f"Google çeviri denemesi başarısız ({attempt}/{self.MAX_RETRIES}): {e}"
                )
                time.sleep(self.RETRY_DELAY * attempt)

        # Fallback dene
        self.logger.info("Google çeviri başarısız oldu, fallback çevirici deneniyor...")
        return self._translate_fallback(clean_text)


_default_translator = None


def get_translator() -> SynopsisTranslator:
    global _default_translator
    if _default_translator is None:
        _default_translator = SynopsisTranslator()
    return _default_translator


def translate_synopsis(text: Optional[str]) -> Optional[str]:
    """Hızlı kullanım için yardımcı fonksiyon."""
    return get_translator().translate(text)
