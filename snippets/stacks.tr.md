- [ ] **İş mantığı bir kez yazılıyor** ve karşılaştırılan her sürüm onu import
      ediyor. Aynı fikrin dört ayrı implementasyonu tablodaki her sayıyı
      bulandırır.
- [ ] **Hiçbir prompt framework'ün alanlarının içinde durmuyor.** Registry
      render ediyor, framework metni alıyor. Lock-in'in pratik ölçüsü budur.
- [ ] **Risk kuralı tek bir sabit ve paylaşılıyor.** Aksi hâlde "onaya düşen"
      sütunu orkestratörleri değil kuralları karşılaştırır.
- [ ] **Çift gönderim outbox'tan sayılıyor**, sürümün kendisi hakkında
      söylediğinden değil. Önemli olan sayı müşteriye ulaşacak olandır.
- [ ] **Onayı, koşuyu başlatandan farklı bir instance veriyor.** Yalnızca kendi
      nesnesiyle onaylanabilen bir sürümün demo dışında human-in-the-loop
      desteği yoktur.
- [ ] **Benchmark'ın modeli deterministik.** Gerçek bir model her sütunu
      framework'le ilgisiz sebeplerle gürültülü yapar.
- [ ] **Model çağrıları herkes için aynı şekilde sayılıyor**: modele giden
      istekler; turn, span ya da her framework'ün kendi telemetrisinin adını
      taktığı şey değil.
- [ ] **Her framework'ün trace'inin nereye gittiği yazılı.** En az biri
      varsayılan olarak bir sağlayıcıya gönderiyor.
- [ ] **Koşu ortasındaki dayanıklılık, gerçekten yazdığın granülariteyle
      belirtiliyor**, framework'ün destekleyebileceğiyle değil.
- [ ] **Karar tablosu her seçeneği *ne zaman kullanmamak* gerektiğini de
      söylüyor.** Kazananı olan ve kısıtı olmayan bir karşılaştırma reklamdır.
- [ ] **Duraklamayı kendi kalıcılaştıran bir framework, onu sana geri
      verenden daha değerli.** Buradaki üç agent SDK'sının ikisi o katmanı
      yazmayı, test etmeyi ve yedeklemeyi sana bırakıyor; karşılaştırma
      hangisinin öyle olduğunu söylemeli.
- [ ] **Temizlik kodu dosyaları değil dizinleri de siliyor.** Dizin olarak
      tutulan bir session `*.sqlite3` glob'undan sağ çıkar ve sonraki koşu
      yanlış sebeple idempotent görünür.
