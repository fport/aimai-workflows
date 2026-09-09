- [ ] **Gate topolojide bir branch, prompt'ta bir cümle değil.** Prompt talimatı
      bir ricadır; incelenen belgenin içindeki metin onunla tartışabilir.
- [ ] **`interrupt()` çağrısının üstünde side effect yok.** Node her resume'da
      ilk satırından itibaren yeniden çalışır, yani o satırın üstündeki her şey
      resume başına bir kez daha tetiklenir.
- [ ] **Idempotency key olgulardan türetiliyor**; UUID'den, deneme sayısından
      ya da saatten değil. İçinde timestamp olan bir key, var olma sebebi olan
      deduplication'ı sessizce devre dışı bırakır.
- [ ] **Side effect'in yazıldığı yer `INSERT … ON CONFLICT DO NOTHING`,**
      check-then-write değil. Aynı thread'i aynı anda iki worker'ın resume
      etmesi tam da key'in var olma sebebidir ve check-then-write yarışır.
- [ ] **`durability` koşu başına seçiliyor.** Yeniden başlayabilen bir batch
      için `exit`, insanın beklediği bir akış için `sync`. Farkı exception'la
      değil `SIGKILL` ile ölç.
- [ ] **`thread_id` bir iş anahtarından türetiliyor.** Yeniden denenen
      webhook'lar, olay sonrası devam eden operatörler ve dünkü e-postayı açan
      reviewer iş numarasını bilir; hiçbiri servisin uydurduğu UUID'yi bilmez.
- [ ] **State referans tutuyor, payload değil.** State'te taşınan bir belge her
      superstep'te yeniden yazılır ve onay ne kadar sürerse o kadar kalıcı
      depoda oturur.
- [ ] **Neyin değerlendirildiği, üzerine aksiyon alınmadan önce doğrulanıyor.**
      Gate günlerce açık kalır; URI'nin arkasındaki dosya o sürede değişebilir.
- [ ] **State'teki her anahtar opsiyonel.** Zorunlu anahtar ekleyen bir deploy
      duraklamış bütün koşuları sahipsiz bırakır.
- [ ] **Serializer'ın bir allowlist'i var.** Bir checkpoint'i okumak, byte'ların
      adını verdiği tipi inşa etmek demektir.
- [ ] **Checkpointer süreç başına bir kez açılıyor** — uygulamanın lifespan'inde,
      istek başına değil.
- [ ] **Öldürülen worker test ediliyor, varsayılmıyor.** `SIGTERM` bir shutdown
      hook'unun çalışmasına izin verir; gerçekte olan `SIGKILL`'dir.
