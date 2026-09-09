# Ölçümler

Bu sayfadaki her tablo depodaki bir script tarafından üretiliyor ve hepsi API
anahtarı, veritabanı ve ağ olmadan koşuyor. Komutlar her bölümde yazılı; CI
sabitlenmiş olanları her push'ta yeniden üretiyor — yayınlanmış bir sayıyı
sessizce geçersiz kılan bir değişiklik, bir okuyucu fark etmeden build'i
düşürüyor.

!!! warning "Buradaki bir rakamı alıntılamadan önce bunu oku"

    **Modeller stub.** Sözleşme reviewer'ı kalıp eşliyor, destek modeli regex
    ile triage yapıyor, uzmanlar yazılı transcript'leri tekrarlıyor. Gerçek iş
    yapıyorlar ve hiçbiri bir modelin işi değil.

    Bu bilinçli. İddialar koordinasyon hakkında — öldürülen bir worker devam
    eder, onaysız aksiyon imkânsızdır, yeniden çalıştırma bedavadır — ve böyle
    bir iddianın her push'ta, CI'da, kimlik bilgisi ve varyans olmadan
    doğrulanabilmesi gerekir. Aşağıdaki iki satır ölçülmüş değil **simüle**, ve
    göründükleri her yerde — üreten script dahil — bunu söylüyorlar.

    Uydurulmuş bir p95'i sessizce gözlem gibi sunan bir tablo, hiç tablo
    olmamasından daha değersizdir.

---

## 06 — dayanıklılık

```bash
uv run python scripts/measure.py --runs 100            # SQLite
uv run python scripts/measure.py --postgres --runs 100 # gerçek veritabanı
```

### Üç `durability` modu

Aynı inceleme — başlat, gate'te dur, onayla — her modda.

| Mod | Uçtan uca (s) | Checkpoint yazma | Superstep | Değerlendirme sırasında `SIGKILL` sonrası |
|---|---|---|---|---|
| `exit` | 0,083 | 2 | 2 | hiçbir şey kalmadı; inceleme yeniden başlıyor |
| `async` | 0,006 | 6 | 6 | `assess_risk`'ten devam ediyor |
| `sync` | 0,018 | 6 | 6 | `assess_risk`'ten devam ediyor |

Exception bu karşılaştırma için yanlış araç: LangGraph onu yakalıyor ve
elindekini kalıcılaştırıyor, dolayısıyla üç mod da aynı görünüyor. `SIGKILL`
dürüst test ve gerçekte olan da o.

### 100 inceleme

| Metrik | Nasıl ölçüldü | SQLite | PostgreSQL |
|---|---|---|---|
| Resume başarı oranı | tamamlanan `Command(resume=…)` / inceleme | 1,000 | 1,000 |
| Replay oranı | değerlendirme node'u iki kez çalışan inceleme | 0,000 | 0,000 |
| Ortalama state boyutu | thread başına serileştirilmiş state | 3.402 B | 3.402 B |
| En büyük state | aynı, en kötü thread | 4.963 B | 4.963 B |
| Checkpoint yazma latency p95 | `put` başına, `sync` modda | 0,231 ms | 1,009 ms |
| Checkpoint yazma | koşu boyunca toplam | 566 | 566 |
| İnceleme başına süre | duvar saati / inceleme | 3,8 ms | 9,4 ms |
| Onay bekleme p50 | **SİMÜLE**, gözlem değil | 2,91 sa | — |
| Onay bekleme p95 | **SİMÜLE**, gözlem değil | 18,07 sa | — |
| İnsan itiraz oranı | **SİMÜLE**, gözlem değil | 0,240 | — |

Simüle olan üç satır, haftalarca gerçek e-postalara cevap veren bir insana
ihtiyaç duyuyor. Sabit seed'li bir lognormal dağılımdan geliyorlar ve
`results/measurements.md` içinde, `scripts/measure.py` içinde ve burada bu
şekilde işaretliler.

### 100 inceleme Postgres'te ne bırakıyor

| Tablo | Boyut | Satır |
|---|---|---|
| `checkpoint_writes` | 1.112 KiB | 1.856 |
| `checkpoints` | 832 KiB | 596 |
| `checkpoint_blobs` | 344 KiB | 211 |

İnceleme başına kabaca 23 KiB. Pending-writes tablosunun en büyük olması
görülmeden anlaşılmıyor: her superstep, commit etmeden önce yapmayı planladığı
yazmaları kaydediyor ve duraklamış bir akış o satırları onay ne kadar sürerse o
kadar tutuyor. Boyuttan çok şekli önemli — belge boyutuyla değil superstep
sayısıyla büyüyor, yani saklama politikası plana girmeli.
`delete_thread(thread_id)` bunun aracı.

### Çökme script'i

```bash
uv run python scripts/kill_mid_run.py
```

```json
{
  "seconds_to_gate": 0.379,
  "worker_returncode": -9,
  "next_before_resume": ["human_gate"],
  "findings_recovered": 13,
  "notes_before_resume": 0,
  "notes_after_resume": 1,
  "passed": true
}
```

---

## 07 — beş stack

```bash
uv run stack-bench                              # results/bench.md
uv run python -m aimai_workflows.stacks.chaos   # results/chaos.md
```

### 50 talep, her stack için üç geçiş

| Stack | tamamlanan | onaya düşen | restart sonrası devam | çift gönderim | llm çağrısı | orkestrasyon satırı | saniye |
|---|---|---|---|---|---|---|---|
| saf Python | 50 | 15 | 15 | 0 | 100 | 160 | 0,20 |
| LangGraph | 50 | 15 | 15 | 0 | 100 | 151 | 0,20 |
| pydantic-ai | 50 | 15 | 15 | 0 | 300 | 259 | 0,42 |
| OpenAI Agents SDK | 50 | 15 | 15 | 0 | 300 | 255 | 0,40 |
| Strands Agents | 50 | 15 | 15 | 0 | 300 | 255 | 0,51 |

`tamamlanan`, outbox'ta en az bir cevabı olan talepleri sayıyor; `çift
gönderim` müşterinin alacağı fazla satırları. İkisi de bir stack'in kendisi
hakkında söylediğinden değil, mesaj deposundan okunuyor.

`orkestrasyon satırı` docstring, yorum ve boş satırları hariç tutuyor — yorum
satırlarını saymak, en çok açıklamaya ihtiyacı olan sürümü ödüllendirirdi.

### İki noktada `SIGKILL`

| Stack | Gate'te öldürüldü → onaylandı mı? | Gönderilen cevap | Duraklamış state | Koşu ortasında öldürüldü → devam? | Tekrarlanan model çağrısı |
|---|---|---|---|---|---|
| saf Python | evet | 1 | 461 B | evet | 1 |
| LangGraph | evet | 1 | 4.966 B | evet | 1 |
| pydantic-ai | evet | 1 | 4.364 B | evet | 2 |
| OpenAI Agents SDK | evet | 1 | 11.427 B | evet | 2 |
| Strands Agents | evet | 1 | 5.996 B | evet | 0 |

`Duraklamış state`, her stack'in duraklamış tek bir talep için yazdığı bütün
text ve blob kolonları ile varsa JSON session dosyalarının boyutu — bir koşuyu
insan beklerken tutmanın maliyeti. Uçta yirmi beş katı depolama: 50 talepte
önemsiz, 50.000 açık onayda konuşulacak bir konu.

`Tekrarlanan model çağrısı`, devralan süreçte paylaşılan iş mantığına yapılan
çağrıları sayıyor. İki, akışın baştan başladığı; bir, öldürmeden önceki işin
yeniden kullanıldığı; **sıfır ise biten her tool sonucunun hayatta kaldığı**
anlamına geliyor — bunu yalnızca Strands başarıyor, çünkü session manager her
sonucu düştüğü anda kalıcılaştırıyor.

---

## 08 — DAG runner

```bash
uv run python scripts/measure_dag.py --jobs 100   # results/dagrun.md
```

Deterministik hata karışımıyla 100 iş: kabaca altıda biri emsali kaybediyor,
yirmi beşte biri teslimattan sonraki kaydı, yüzde biri geri çekmeyi
yapamıyor.

| Node | succeeded | degraded | failed | skipped | compensated |
|---|---|---|---|---|---|
| `extract` | 100 | 0 | 0 | 0 | 0 |
| `scope_gate` | 100 | 0 | 0 | 0 | 0 |
| `clause_review` | 100 | 0 | 0 | 0 | 0 |
| `statute` | 100 | 0 | 0 | 0 | 0 |
| `caselaw` | 87 | 0 | 13 | 0 | 0 |
| `synthesis` | 87 | 13 | 0 | 0 | 0 |
| `deliver` | 80 | 13 | 0 | 0 | 7 |
| `archive` | 77 | 13 | 10 | 0 | 0 |

| Metrik | Nasıl ölçüldü | Değer |
|---|---|---|
| Temiz iş | bütün sink node'ları başarılı | 77/100 |
| Degrade iş | kısmi kanıtla teslim edildi | 13/100 |
| Başarısız iş | bir sink node düştü ya da atlandı | 7/100 |
| İnsan gerekiyor | bir telafi düştü | 3/100 |
| Yeniden çalıştırma tasarrufu | 1 − (yeniden koşunun harcaması / sonucun maliyeti) | %100 |
| İş başına maliyet, medyan | | 0,0780 $ |
| İş başına maliyet, p90 | bütçenin belirlendiği sayı | 0,0780 $ |
| İş başına maliyet, maksimum | tek iş, en kötü durum | 0,0780 $ |

!!! note "Bu tablonun raporlamayı reddettiği iki şey"

    **Ortalama maliyet yok.** Bir supervisor döngüsü ya da bir retry fırtınası
    ortalamayı bozmadan maksimumu katlar; ortalaması olup kuyruğu olmayan bir
    tablo, fatura geldiğinde önemli olan tek sayıyı gizler.

    **Toplam başarı oranı yok.** "İşlerin %94'ü başarılı" ifadesi, emsal
    uzmanının bütün hafta kapalı olmasıyla uyumludur — her iş degrade olur,
    hiçbiri düşmez ve toplam iyi görünür. Düğüm başına oranlar kesintiyi
    görünür kılar.

Maliyet satırlarına uygulanacak iki uyarı: uzmanlar model değil scripted
ajanlar, p90'ın maksimuma eşit olmasının sebebi bu — gerçek bir modelin maliyet
dağılımı bu tabloda yok. Ve %13'lük degradation oranı script'in enjekte ettiği
hata karışımı, gerçek bir servis hakkında bir gözlem değil.

---

## CI neyi sabitliyor, neyi sabitlemiyor

`results/durability.json`, `results/bench.json` ve `results/dagrun.json`
yalnızca **deterministik** olguları tutuyor: checkpoint yazma sayıları,
superstep'ler, replay oranları, orkestrasyon satır sayıları, çift gönderim
sayıları, düğüm başına durum sayıları. CI bunları yeniden üretiyor ve bir diff
olursa düşüyor; yani koşu başına yazmaları ikiye katlayan ya da bir resume'un
değerlendirmeyi yeniden çalıştırmasına yol açan bir refactor, bir okuyucu
tarafından değil bir diff tarafından yakalanıyor.

Markdown tabloları süreleri de tutuyor. Onlar makineden makineye değişiyor, bu
yüzden commit ediliyorlar ama diff'lenmiyorlar.
