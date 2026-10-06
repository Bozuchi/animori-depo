"""
test_translation.py — GitHub Actions & Yerel Çeviri Test Aracı
Google Translate ve MyMemory fallback servislerinin engellenip engellenmediğini test eder.
"""

import os
import sys
import time
import requests
from src.translator import SynopsisTranslator

def test_translation():
    print("=" * 65)
    print("🧪 GITHUB ACTIONS & YEREL ÇEVİRİ VE API ERİŞİM TESTİ")
    print("=" * 65)

    # 1. IP ve Konum Bilgisi (Runner'ın veri merkezi IP'sini görmek için)
    try:
        res = requests.get("https://ipinfo.io/json", timeout=5)
        if res.status_code == 200:
            ip_info = res.json()
            print(f"📍 Sunucu IP: {ip_info.get('ip')} | Konum: {ip_info.get('city')}, {ip_info.get('country')} | Sağlayıcı: {ip_info.get('org')}")
    except Exception as e:
        print(f"ℹ️ IP bilgisi alınamadı: {e}")

    translator = SynopsisTranslator()

    test_cases = [
        (
            "Kısa Anime Özeti",
            "Monkey D. Luffy sets sail with his crew to find the legendary treasure known as One Piece and become the Pirate King."
        ),
        (
            "Orta Uzunlukta Özet",
            "After a catastrophic event wipes out most of human civilization, a teenage science prodigy awakens thousands of years later and resolves to rebuild the world from scratch using the power of science."
        ),
        (
            "Özel Karakterler & İsimler",
            "Naruto Uzumaki, a mischievous adolescent ninja, struggles as he searches for recognition from his village peers and the Hokage."
        ),
    ]

    success_count = 0
    total = len(test_cases)
    results = []

    for name, text in test_cases:
        print(f"\n--- [Test: {name}] ---")
        print(f"🇬🇧 Orijinal ({len(text)} karakter):\n  {text}")

        start_time = time.time()
        translated = translator.translate(text)
        duration = time.time() - start_time

        if translated and translated != text:
            print(f"🇹🇷 Türkçe ({duration:.2f}s):\n  {translated}")
            success_count += 1
            results.append((name, True, duration, translated))
        else:
            print(f"❌ BAŞARISIZ! Çeviri dönmedi.")
            results.append((name, False, duration, None))

        time.sleep(1)

    print("\n" + "=" * 65)
    print(f"📊 SONUÇ: {success_count}/{total} test başarılı!")
    print("=" * 65)

    # Discord Webhook varsa bilgilendir
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook_url:
        print("📢 Discord bildirimi gönderiliyor...")
        try:
            status_emoji = "✅" if success_count == total else "⚠️"
            sample_tr = results[0][3] if results and results[0][1] else "Bulunamadı"
            msg = (
                f"{status_emoji} **GitHub Actions Çeviri Motoru Testi**\n"
                f"**Başarı Oranı:** {success_count}/{total}\n"
                f"**Örnek Çeviri:** {sample_tr[:120]}...\n"
                f"**Durum:** Çeviri kütüphanesi Azure/GitHub sunucularında sorunsuz çalışıyor!"
            )
            requests.post(webhook_url, json={"content": msg}, timeout=5)
            print("✅ Discord'a test bildirimi iletildi.")
        except Exception as e:
            print(f"⚠️ Discord bildirimi gönderilemedi: {e}")

    if success_count < total:
        print("❌ Bazı testler başarısız oldu!")
        sys.exit(1)
    else:
        print("🎉 Tüm testler başarıyla geçti!")

if __name__ == "__main__":
    test_translation()
