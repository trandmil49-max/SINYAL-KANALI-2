# Telegram Binance Futures Signal Bot

Bu proje Binance USDT perpetual futures piyasasini public Binance API ile tarar ve filtrelerden gecen yuksek guven skorlu LONG/SHORT sinyallerini Telegram'a gonderir.

Bot emir acmaz, para yonetmez ve Binance API key istemez. Sadece analiz ve bildirim yapar.

## Ozellikler

- Dinamik Binance USDT perpetual sembol tarama
- Likidite ve hacim filtresi
- BTC market sagligi kontrolu
- Trend, momentum, volume, market structure ve risk skoru
- Funding rate ve open interest kontrolu
- Telegram komutlari
- SQLite sinyal kaydi
- Tekrar sinyal cooldown korumasi ve gunluk coin basina sinyal limiti
- Acik pozisyonlar icin otomatik TP1 / TP2 / Stop Loss takibi ve bildirimi
- `/status` icinde basit kazanma orani (TP2 vs SL) ozeti
- Windows ve Linux/macOS calistirma dosyalari

## TP1 / TP2 / Stop Loss Bildirimleri

Bot artik gonderdigi her sinyali veritabaninda "acik pozisyon" olarak isaretler ve
`POSITION_CHECK_INTERVAL_SECONDS` degerinde belirtilen surede bir (varsayilan 30 sn)
Binance'ten guncel fiyati cekip kontrol eder:

- Fiyat TP1'e ulasirsa TP1 bildirimi gelir, pozisyon TP2/SL icin izlenmeye devam eder.
- Fiyat TP2'ye ulasirsa TP2 bildirimi gelir ve pozisyon kapanir.
- Fiyat Stop Loss'a ulasirsa SL bildirimi gelir ve pozisyon kapanir.

Not: Bot sadece analiz/bildirim yapar, gercek bir pozisyon acmaz; bu takip tamamen
kagit uzerinde (paper) fiyat karsilastirmasidir.

## Otomatik "Yeni Sinyal Yok" Mesajlari

Varsayilan olarak otomatik taramalarda "Tarama tamamlandı, yeni sinyal yok" mesaji
**artik gonderilmez** (spam onlemek icin). Sadece `/scan` komutuyla elle tarama
yaptiginizda bu bilgi mesajini gorursunuz. Otomatik taramalarda da bu mesaji almak
isterseniz `.env` icinde `ANNOUNCE_EMPTY_SCANS=true` yapabilirsiniz.

## Gunluk Sinyal Limiti

Ayni coin icin (yon farketmeksizin) 24 saat icinde en fazla kac sinyal gonderilecegi
`MAX_SIGNALS_PER_SYMBOL_PER_DAY` ile kontrol edilir (varsayilan: 2). Bu, ayni coin'in
LONG ve SHORT arasinda surekli sinyal uretmesini engeller.

## Kurulum

1. Python 3.10 veya daha yeni bir surum kurulu olmali.
2. Telegram'da BotFather ile yeni bot token al.
3. Botu mesaj gonderecegin kullaniciya, gruba veya kanala ekle.
4. `.env.example` dosyasini `.env` adiyla kopyala.
5. `.env` dosyasindaki degerleri doldur.

Windows:

```powershell
copy .env.example .env
notepad .env
```

Linux/macOS:

```bash
cp .env.example .env
nano .env
```

## Calistirma

Her sistemde ana calistirma komutu:

```bash
python bot.py
```

Windows icin:

```powershell
run_bot.bat
```

Linux/macOS icin:

```bash
chmod +x run_bot.sh
./run_bot.sh
```

## Telegram Komutlari

- `/start` - botun aktif oldugunu gosterir
- `/status` - son tarama ve sistem durumunu gosterir
- `/scan` - hemen yeni tarama baslatir
- `/help` - komutlari listeler

## Ayarlar

`.env` icindeki onemli alanlar:

- `TELEGRAM_BOT_TOKEN`: BotFather tarafindan verilen bot token
- `TELEGRAM_CHAT_ID`: Mesaj gonderilecek Telegram chat id
- `MIN_CONFIDENCE`: Sinyal gondermek icin minimum skor
- `MAX_SYMBOLS_TO_ANALYZE`: Hacme gore analiz edilecek maksimum sembol sayisi
- `MIN_QUOTE_VOLUME_USDT`: Minimum 24 saatlik USDT hacmi
- `SCAN_INTERVAL_SECONDS`: Otomatik tarama araligi
- `SIGNAL_COOLDOWN_MINUTES`: Ayni coin ve yon icin tekrar sinyal bekleme suresi

## Saat Gosterimi

Sinyal mesajlarindaki saat artik Turkiye yerel saatine (Europe/Istanbul) gore
gosterilir, boylece mesaj icindeki saat ile Telegram'in mesaj balonunda gosterdigi
teslim saati birbiriyle uyusur. Onceki surumde mesaj UTC saatini yaziyordu; bu,
gercekte bir gecikme olmadigi halde "sinyal 16:29'da olmus ama bana 19:29'da
gelmis" seklinde bir kafa karisikligina yol aciyordu (Turkiye UTC+3 oldugu icin
ikisi ayni andi).

## Skor Yuksek Ama Islem Kaybediyor

Confidence skoru (orn. 86/100), sinyalin filtrelerden ne kadar guclu gectigini
gosterir; gelecekteki fiyat hareketinin garantisi degildir. Skor yuksek olsa bile
piyasa haber, likidasyon dalgasi gibi sebeplerle stop'a gidebilir. `/status`
komutundaki TP2/SL kazanma orani zamanla gercek performansi gormenizi saglar;
bu oran dusukse `MIN_CONFIDENCE` degerini yukseltmek, `SIGNAL_COOLDOWN_MINUTES`
ve TP/SL mesafelerini (risk/reward) ayarlamak denenebilir. Bu bir strateji/backtest
konusu oldugu icin kod tarafinda "kesin cozum" yoktur; zaman icinde biriken
TP2/SL istatistiklerine bakarak ayar yapmaniz gerekir.

## Guvenlik

`.env` dosyasini GitHub'a yukleme. Bu dosyada Telegram token bulunur. Repoya sadece `.env.example` yuklenmelidir.

## Risk Uyarisi

Bu proje yatirim tavsiyesi degildir. Futures islemleri yuksek risk tasir. Bot sadece analiz ve bildirim aracidir.
