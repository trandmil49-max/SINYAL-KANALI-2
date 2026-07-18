# Telegram Binance Futures Signal Bot

Bu proje Binance USDT perpetual futures piyasasini public Binance API ile tarar ve filtrelerden gecen yuksek guven skorlu LONG/SHORT sinyallerini Telegram'a gonderir.

Bot emir acmaz, para yonetmez ve Binance API key istemez. Sadece analiz ve bildirim yapar.

## Ozellikler

- Dinamik Binance USDT perpetual sembol tarama — varsayilan olarak filtreyi gecen TUM pariteler taranir (sadece hacme gore ilk N tanesi degil)
- Likidite ve hacim filtresi
- BTC market sagligi kontrolu
- 15dk / 1sa / 4sa cok-zaman-dilimli trend, momentum, volume, market structure ve risk skoru
- **Sert filtreler (hard gate):** trend, yapi, ADX, 4H uyumu, BTC durumu ve "zaten pompalanmis coin" kontrolu artik puan kaybettirmiyor, direkt eliyor — asagida detayli anlatildi
- **Risk-normalize edilmis kaldirac onerisi:** stop mesafesi ne kadar genisse onerilen kaldirac o kadar dusuk, boylece bir SL'in en kotu ihtimalle verdirecegi kayip yuzdesi hep benzer bir araligin altinda kalir
- Cok genis stop gerektiren (asiri oynak) coin'ler artik hic sinyal olarak gonderilmiyor
- **ABD borsa acilis penceresinde (TR saatiyle, DST'ye gore otomatik ayarlanir) yeni sinyal aranmaz** — mevcut acik pozisyonlar bu sirada normal takip edilmeye devam eder
- Funding rate ve open interest kontrolu
- Telegram komutlari
- SQLite sinyal kaydi
- Tekrar sinyal cooldown korumasi, gunluk coin basina sinyal limiti VE acik pozisyon varken ayni coin'e tekrar sinyal atmama korumasi
- Acik pozisyonlar icin bagimsiz bir arka plan thread'inde otomatik TP1 / TP2 / Stop Loss takibi ve bildirimi (tarama surerken bile gecikmez)
- TP1 vurulunca stop otomatik olarak basabasa (giris fiyatina) cekilir — kazanilan kar geri kaybedilmez
- Turkiye saatiyle tam 23:00'da otomatik gunluk / haftalik / aylik ozet raporlari
- `/status` icinde basit kazanma orani (TP2 vs SL) ozeti
- Windows ve Linux/macOS calistirma dosyalari

## Kaldıraçlı Kayıp Çok Büyüktü (1000XECUSDT örneği) — Düzeltildi

Bir örnekte stop mesafesi fiyatın %7.28'i kadardı ve bot 5x kaldıraç önermişti
— bu da tek bir SL'de **%36.38** kaldıraçlı kayıp demekti. Bunun sebebi,
kaldıraç önerisinin sadece kaba araliklara (0-2.5%, 2.5-4.5%, ...) gore sabit
bir sayı vermesiydi; genis bir stop ile bile göreceli olarak yüksek kaldıraç
önerilebiliyordu.

Artık kaldıraç, **sabit bir hedef kayıp yüzdesine** (yaklaşık %12) göre stop
mesafesinden ters orantılı hesaplanıyor: stop ne kadar genişse kaldıraç o
kadar düşük öneriliyor (2x ile 10x arasında sınırlı). Aynı 1000XECUSDT
senaryosunu (aynı %7.28 stop mesafesiyle) yeniden hesapladığımda artık 5x
yerine **2x** öneriyor — en kötü ihtimal kaybı %36'dan **%14.5**'e düştü.

Ayrıca, stop mesafesi fiyatın **%8'ini** asan (asiri oynak/riskli) coin'ler
için artık **hiç sinyal üretilmiyor** — bu kadar genis bir stop, dusuk
kaldiracla bile risk yonetimi acisindan mantikli degil, o yuzden boyle
coin'ler tamamen atlaniyor.

## ABD Borsa Açılışında Piyasanın Ters Dönmesi

Gözlem dogruydu: ABD borsasi acildiginda (spot BTC ETF akislarindan dolayi
NASDAQ/SPX ile artan korelasyon), kripto piyasasi sik sik gunun geri
kalaninda olusan yonu aniden tersine cevirebiliyor.

**Durustce belirtmem gerekiyor:** Bu ortamda internete cikisim kapali, yani
NASDAQ/SPX endekslerini canli cekip botun buna gore karar vermesini saglayan
bir entegrasyonu yazip test edemezdim — boyle bir seyi test etmeden teslim
etmek tam olarak sizin istemediginiz sey. Onun yerine, canli endeks verisi
yerine **test edilebilir** bir onlem ekledim: ABD borsasinin acilis saatini
(9:30 ET, Turkiye saatine yaz/kis saatine gore otomatik cevrilir — bu kismi
gercekten test ettim, hem yaz hem kis saati icin dogru sonuc veriyor) botun
kendisi biliyor, ve bu saatin etrafinda (09:15–10:30 ET, yani yaz saatinde TR
16:15–17:30, kis saatinde TR 17:15–18:30) **yeni sinyal aramiyor**. Acik
pozisyonlar bu pencerede de normal sekilde TP/SL icin izlenmeye devam eder,
sadece yeni sinyal uretimi durur.

Bu, ABD piyasa tatillerini hesaba katmiyor (ornegin resmi tatil gunlerinde
borsa kapali olsa bile bot yine de bu saatte sinyal aramayi durdurur — zararsiz
ama gereksiz bir duraklama). Gercek NASDAQ/SPX korelasyon takibi ileride
istenirse, guvenilir bir veri kaynagi (API key gerektirebilir) belirtilip
gercek ortamda test edilerek eklenebilir.

## Az Ama Kaliteli Sinyal Odagi — Onemli Hatirlatma

Bu turdaki degisiklikler ikisi de (kaldirac normalizasyonu + ABD acilis
penceresi) somut, test edilmis ve dar kapsamli. Gecen sefer "teknik analiz"
tarafinda buyuk bir revizyon yapilmisti (sert filtreler, pompa-kovalama
reddi); bu sefer bilerek o kisimlara tekrar dokunulmadi — ayni parametreleri
her gun degistirmek yerine, her degisikligin gercekten etkisini gormek icin
biraz zaman tanimak daha saglikli. `/rapor` ve gunluk ozetlerle sonuclari
takip edip, hangi ayarin gercekten ise yaradigini birlikte degerlendirebiliriz.

## Teknik Analiz Koklu Degisiklik: Az Ama Kaliteli Sinyal

Onceki sistemde her kriter (trend, yapi, ADX, 4H uyumu, BTC durumu) sadece puan
kazandiriyor/kaybettiriyordu — yani bir coin trend'i tutmasa bile diger
kriterlerden topladigi puanla yine de esik degeri (confidence) gecebiliyordu.
Bu, "bazi seyler uyuyor" seviyesindeki zayif kurulumlarin bile sinyal olarak
gitmesine yol aciyordu — cok fazla sinyal, dusuk isabet orani.

Artik asagidaki kriterlerin **HEPSI** ayni anda saglanmadan bir coin
degerlendirmeye bile alinmiyor (puan degil, direkt eleme):

- **Trend** (15dk/1sa EMA yapisi) yon ile uyumlu olmali
- **Piyasa yapisi** (higher-high/higher-low vb.) yon ile uyumlu olmali
- **ADX >= 20** olmali (piyasa gercekten trend'de olmali, yatay/kararsiz
  piyasada sinyal uretilmez)
- **4 saatlik zaman dilimi** ters yonde olmamali
- **BTC durumu** "Dangerous" olmamali
- **YENI: "Zaten pompalanmis coin" filtresi** — coin son 6 saatte zaten
  %12'den fazla hareket ettiyse (yon ile ayni yonde), o coin icin sinyal
  **uretilmez**. Ekran goruntulerinde gordugumuz PUMPUSDT ve USUSDT
  kayiplari tam olarak bu desene uyuyordu: fiyat zaten dikine firlamisken
  bot buna "momentum/hacim onaylandi" diyerek atlamaya calisiyordu — halbuki
  bu, hareketin baslangici degil cogu zaman sonunun isaretiydi. Artik boyle
  bir kurulum otomatik olarak elenir.

Bu kriterlerin hepsini ayni anda gecen kurulumlar zaten azdir, bu yuzden
`MIN_CONFIDENCE` varsayilani da 82'den **86**'ya cekildi (ek bir guvenlik
katmani olarak). Sonuc: gunluk sinyal sayisinin belirgin sekilde azalmasi ve
kalan sinyallerin cok daha secici filtrelerden gecmis olmasi beklenir.

**Durustce soylemek gerekirse:** Hicbir kural seti belirli bir kazanma
oranini (orn. %70-80) garanti edemez — kripto piyasasi tahmin edilemez
kalmaya devam eder ve gercek zararli haberler / ani likidasyon dalgalari her
zaman olabilir. Bu degisiklik "botun piyasayi okuma kalitesini" somut,
gerekce lendirilmis kriterlerle yukseltiyor (ozellikle pompalanmis coin
kovalama sorununu kokten kapatiyor), ama garanti degil. `/rapor` ve gunluk
ozetlerle birkac gun/hafta izleyip sonuclari gormek en dogrusu.

## Ayni Coin'e Tekrar Sinyal Sorunu Hala Goruluyorsa — ONEMLI

Kod tarafinda "acik pozisyon varken o coin'e yeni sinyal atma" korumasi zaten
var ve test edildi (bkz. asagidaki "Ayni Coin'e Ust Uste Sinyal Sorunu"
bolumu) — ayni anda calisan TEK bir bot process'i icin bu koruma %100
calisir. Ama ekran goruntusunde ayni dakika icinde SUIUSDT ve LTCUSDT icin
neredeyse ayni (fiyati bir tik farkli) iki ayri sinyal gorduk. Bunun kod
hatasi degil, **altyapi (deploy) kaynakli** olma ihtimali cok yuksek. Iki
olasi sebep:

1. **`signals.db` kalici (persistent) bir diskte durmuyor olabilir.** Bircok
   deploy platformu (Railway dahil) her yeniden deploy'da veya container
   yeniden baslattiginda dosya sistemini sifirlar — eger `signals.db` icin
   ozel bir "Volume" (kalici disk) baglanmadiysa, her redeploy'da veritabani
   bombos baslar. Bu durumda bot, az once ayni coin icin actigi pozisyonu
   "hatirlayamaz" ve coin'i sanki hic sinyal atilmamis gibi tekrar
   degerlendirir. Deploy panelinde "Volume" / "Persistent Disk" / "Storage"
   gibi bir ayar arayip `signals.db`'nin bulundugu klasore kalici bir disk
   baglamaniz gerekebilir.
2. **Ayni bot ayni anda birden fazla kopya (replica/instance) olarak
   calisiyor olabilir.** Deploy platformunda "Replicas" veya "Instances"
   sayisi 1'den fazlaysa, her kopyanin **kendi ayri** `signals.db`'si olur;
   biri bir coin icin sinyal actiginda digeri bundan habersiz oldugu icin
   ayni coin'e kendi tarafindan da sinyal atabilir. Daha once yasadigimiz
   Telegram "409 Conflict" hatasi da aslinda bunun bir belirtisi olabilir.
   Deploy panelinde replica/instance sayisinin **1** oldugunu kontrol edin.

Bu ikisi de kod degisikligiyle degil, deploy/altyapi ayarlarinizla cozulur;
kod zaten tek bir process icin dogru calisiyor (test edildi).

## Ayni Coin'e Ust Uste Sinyal Sorunu — Duzeltildi

Bot, bir coin'de zaten acik (henuz TP2/SL/basabas ile kapanmamis) bir pozisyon
varken, o coin filtrelerden tekrar gecse bile **artik yeni bir sinyal atmiyor**.
Onceki davranista sadece cooldown (4 saat, ayni yon icin) ve gunluk limit
kontrolu vardi; bu, ayni coin icin eski sinyal hala takip edilirken yeni bir
sinyal daha gonderilmesine ve gereksiz yere cok fazla sinyal uretilmesine (orn.
bir gunde 62 sinyal) yol aciyordu. Artik kural basit: **bir coin'de acik
pozisyon varsa, o kapanana kadar o coin icin yeni sinyal yok.** Pozisyon TP2,
SL veya basabas ile kapandiginda coin tekrar sinyal alabilir hale gelir. Bu,
toplam sinyal sayisini da dogal olarak onemli olcude azaltir.

## TP1 Sonrasi Basabas (Breakeven) Stop — Duzeltildi

Onceki davranista, TP1 vurulup kar alindiktan sonra fiyat geri donup orijinal
stop seviyesine giderse pozisyon **tam SL** olarak kapaniyordu — yani TP1'de
kazanilan kar, sonrasinda tamamen geri veriliyordu. Bu, raporlarda "kazandigim
parayi baska islemlerde geri verdim" seklinde goze carpan sorunun sebebiydi.

Artik TP1 vuruldugu anda stop otomatik olarak **giris fiyatina (basabas)**
cekiliyor. Fiyat sonra geri donerse pozisyon basabas civarinda kapanir —
TP1'de alinan kar korunmus olur, tam kayip degil. Bu kapanis turu raporlarda ayri
bir kategori olarak gosterilir:

- 🎯 **TP2** — hedefe tam ulasti, tam kazanc
- ⚪️ **Basabas (TP1 sonrasi)** — TP1'de kar alindi, sonra basabasa donuldu; net
  sonuc kabaca notr, tam kayip degil
- 🛑 **SL** — TP1'e hic ulasilmadan orijinal stop'a gidildi, gercek kayip

Raporlardaki "Kazanma orani" hesabi sadece TP2 ve gercek SL'i karsilastirir,
basabas islemler bu orana dahil edilmez (ne kazanc ne kayip sayildigi icin).

## Stop Loss Cok Sik Tetikleniyordu — Duzeltildi

Raporlarda SL orani cok yuksek cikiyordu (orn. 27/36 kapanan islem SL). Sebebi
bulundu: stop mesafesi **15 dakikalik mumun ATR'inin sadece 1.35 kati** olarak
hesaplaniyordu — bu, sikca normal fiyat "gurultusunden" (gercek bir don us
olmadan, sadece kisa vadeli inis-cikistan) bile daha dar bir mesafeydi. Coin
dogru yonde gitse bile, hareket gelismeden once stop'a carpiyordu.

Artik stop mesafesi cok daha az "gurultulu" olan **1 saatlik ATR**'a gore
hesaplaniyor (`atr_1h * 1.6`), TP1/TP2 hedefleri de bu daha genis birime gore
olculuyor (risk/reward orani ayni kaliyor, ~2.25). Onerilen kaldirac da artik
gercek stop mesafesine gore hesaplaniyor (stop ne kadar genisse, onerilen
kaldirac o kadar dusuk).

Bu bir "garanti kazanc" duzeltmesi degildir — piyasa yine de yanlis yonde
gidebilir. Ama artik kayiplarin en azindan **gercek bir ters hareketten**
kaynaklanmasi beklenir, dar bir stop'un normal gurultuyle tetiklenmesinden
degil. Birkac gunluk/haftalik raporu (`/rapor` veya otomatik 23:00 raporlari)
takip ederek kazanma oraninin nasil degistigini gorebilirsiniz; hala dusukse
`MIN_CONFIDENCE` degerini yukseltmek bir sonraki adim olabilir.

## Gunluk / Haftalik / Aylik Ozet Raporlari

Bot, Turkiye saatiyle (Europe/Istanbul) her gun **tam 23:00**'da otomatik olarak
o gune ait bir ozet mesaji gonderir. Ayrica:

- Haftanin son gunu (**Pazar**) saat 23:00'da, o haftaya ait bir haftalik ozet de eklenir.
- Ayin son gunu saat 23:00'da, o aya ait bir aylik ozet de eklenir.

Yani ayin son gunu Pazar'a denk gelirse ayni anda 3 rapor (gunluk + haftalik +
aylik) art arda gelir — bu normaldir.

Her rapor sunlari icerir:

- Donemde gonderilen toplam sinyal sayisi (LONG / SHORT dagilimi)
- Kac tanesi TP2 ile kazandi, kac tanesi Stop Loss ile kaybetti, kac tanesi hala acik
- Kazanma orani (TP2 / (TP2+SL))
- Ortalama confidence skoru
- O donemin en iyi islemi (en yuksek % kazanc) ve en kotu islemi (en buyuk % kayip)

Raporlar veritabaninda ayrica loglanir (`report_log` tablosu), boylece bot yeniden
baslasa bile ayni gun/hafta/ay icin rapor iki kez gonderilmez. `.env` degistirmeye
gerek yok, bu ozellik otomatik aktiftir.

Saat 23:00'i beklemeden test etmek isterseniz Telegram'dan `/rapor` komutunu
gonderin — bu, o gune kadarki verilerle gunluk formatta bir ozeti hemen gonderir
(rapor loguna kaydedilmez, istediginiz kadar tekrar calistirabilirsiniz).

## Tum Piyasayi Tarama

Onceki surumde bot her taramada hacme gore sadece en yuksek `MAX_SYMBOLS_TO_ANALYZE`
(varsayilan 80) pariteyi analiz ediyordu; bu da hep ayni buyuk hacimli birkac
coin'in sinyal uretmesine yol aciyordu. Artik varsayilan `MAX_SYMBOLS_TO_ANALYZE=0`,
yani `MIN_QUOTE_VOLUME_USDT` esigini gecen TUM Binance Futures USDT-M pariteleri
her taramada analiz edilir. Belirli bir sayiyla sinirlamak isterseniz `.env`
icinde bu degeri pozitif bir sayi yapabilirsiniz (orn. 80), ama bu durumda yine
sadece en yuksek hacimli o kadar parite taranir.

## TP1 / TP2 / Stop Loss Bildirimleri

Bot gonderdigi her sinyali veritabaninda "acik pozisyon" olarak isaretler ve
bunlari `POSITION_CHECK_INTERVAL_SECONDS` degerinde belirtilen surede bir
(varsayilan 15 sn) **ayri bir arka plan thread'inde** kontrol eder:

- Fiyat TP1'e ulasirsa TP1 bildirimi gelir, pozisyon TP2/SL icin izlenmeye devam eder.
- Fiyat TP2'ye ulasirsa TP2 bildirimi gelir ve pozisyon kapanir.
- Fiyat Stop Loss'a ulasirsa SL bildirimi gelir ve pozisyon kapanir.

Bu kontrolun ayri bir thread'de calismasi onemlidir: piyasa taramasi (ozellikle
artik tum pariteleri tarayan surumde) uzun surebilir; TP/SL kontrolu ayni
donguye bagli olsaydi, tarama bitene kadar TP/SL mesajlari da gecikirdi. Artik
tarama ne kadar surerse sursun TP/SL kontrolu kendi periyodunda calismaya
devam eder.

Not: Bot sadece analiz/bildirim yapar, gercek bir pozisyon acmaz; bu takip tamamen
kagit uzerinde (paper) fiyat karsilastirmasidir.

## Cok Zaman Dilimli Teknik Analiz

Skorlama artik sadece 15dk/1sa'ya degil, 4 saatlik zaman dilimindeki EMA50/EMA200
yapisina da bakar (buyuk resmi/ana trendi teyit etmek icin). 4 saatlik trend
sinyal yonuyle ayniysa skor artar ("4H Trend Confirmed" olarak mesajda da
gorunur), ters yondeyse skor dusurulur. Bu, botun kisa vadeli gurultuye degil,
daha genis bir zaman dilimindeki gercek trende gore sinyal uretmesini saglar.

## Performans Notu

Tum piyasayi taramak, sinirli sayida parite taramaya gore daha fazla Binance API
cagrisi ve daha uzun tarama suresi demektir (parite basina 15dk/1sa/4sa mum verisi
+ funding + open interest = 5 istek). Yuzlerce parite oldugu icin tam bir tarama
birkac dakika surebilir. `SCAN_INTERVAL_SECONDS` degerinin tarama suresinden kisa
olmamasina dikkat edin; cok kisa tutarsaniz bir tarama bitmeden digeri baslamaya
calisir. Eger tarama surekli cok uzun suruyorsa veya Binance'ten rate-limit hatasi
almaya baslarsaniz `MAX_SYMBOLS_TO_ANALYZE` degerini pozitif bir sayiya (orn. 150)
sabitleyerek taranan parite sayisini sinirlayabilirsiniz.

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

