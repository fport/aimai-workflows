- [ ] **Graf, hiçbir şey çalışmadan önce doğrulanıyor**: tekrar eden id,
      tanımsız referans, çevrim, compensation hedefi, fan-out kaynağı. Hepsi
      başlangıçta bedava, çalışma anında pahalı.
- [ ] **`SKIPPED`, `FAILED`'dan ayrı sayılıyor.** İşi doğru bir kararla
      yapmayan bir koşu, bozulan bir koşu değildir.
- [ ] **Sert ve yumuşak bağımlılıklar ayrı**, ve her kenarın hangisi olduğu
      bilinçli seçilmiş — o karar genellikle mühendislik değil alan kararıdır.
- [ ] **Degradation aşağı akışa yayılıyor ve okuyucuya ulaşıyor.** Eksik girdi
      listesi log'a değil, sentezin *içine* giriyor.
- [ ] **Fingerprint yürütme politikasını dışarıda bırakıyor.** Bir timeout'u
      yükseltmek, başarılı olmuş bir graf dolusu işi yeniden çalıştırmamalı.
- [ ] **Node `version`'ları prompt'tan türetiliyor**, elle tutulmuyor. Kimsenin
      artırmadığı bir integer, artık var olmayan bir prompt'un cevaplarını
      sunan bir cache demektir.
- [ ] **Hatalar kaydediliyor ama cache'ten asla sunulmuyor.** Cache'lenmiş
      geçici bir hata kalıcı bir hatadır.
- [ ] **Bütçe düğümü değil işi durduruyor.** Tavandan sonra iş planlamaya devam
      eden bir runner tavanı birkaç kez harcar.
- [ ] **Compensation, side effect'in kendi node'u düştüğünde de tetikleniyor**,
      yalnızca aşağı akışta bir şey düştüğünde değil. Patlayan bir gönderim,
      gerçekleşmemiş bir gönderim değildir.
- [ ] **Başarısız compensation yeniden denemek yerine yükseltiliyor.** İki
      otomatik kurtarma katmanı bir kötü durumu ikiye çıkarır.
- [ ] **Her `requires` kenarının cevabı değiştirdiği gösterilmiş.**
      Değiştirmeyen bir kenar paralelliğe mal olur ve karşılığında bir şey
      vermez.
- [ ] **Supervisor'ın kısıtları kodda**, prompt'unda değil: tamamlananlar
      adaylardan çıkarılıyor, sert bir adım tavanı var, bilinmeyen bir ad için
      deterministik bir varsayılan var.
- [ ] **Raporlar p90 ve maksimum gösteriyor, asla ortalama değil.** Bir retry
      fırtınası ortalamayı bozmadan maksimumu katlar.
- [ ] **Tek bir başarı oranı değil, düğüm başına oranlar.** "İşlerin %94'ü
      başarılı" ifadesi bir uzmanın bütün hafta kapalı olmasıyla uyumludur.
