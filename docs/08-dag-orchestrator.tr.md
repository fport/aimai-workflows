# 8. Koordinasyon katmanını yazmak

06 ve 07 framework kullandı. Bu aşama koordinasyon katmanını elle yazıyor; o
framework'lerin senin yerine ne yaptığını görmenin tek yolu bu.

Beş node tipi, dokuz durum, yazılı bir state transition tablosu, sert ve yumuşak
bağımlılıklar, fingerprint tabanlı yeniden çalıştırma ve SQLite bir result
store. Üstünde gerçek bir akış ve karşılaştırma için on beş satırlık bir
LangGraph supervisor.

!!! done "Burada ne kurduk"

    Bütün test paketi modelsiz geçen bir runner — kodun en değerli özelliği bu,
    çünkü kanıtlanan şey koordinasyon ve API anahtarı isteyen bir koordinasyon
    testi bir kez koşulur.

## Akış

```text
extract ──> scope_gate ──> statute  ─┐
    │           │                    ├──> synthesis ──> deliver ──> archive
    │           └───────> caselaw ···┘                     ╎
    └─────> clause_review ───────────┘              retract ╌╌ compensates
```

Her node tipi yerini hak ediyor:

| Tip | Node | Neden TASK değil |
|---|---|---|
| `TASK` | `extract`, `statute`, `caselaw` | tek birim iş, dallanma yok |
| `GATE` | `scope_gate` | kapsam dışı bir belge uzmanları **atlamalı**, düşürmemeli |
| `FANOUT` | `clause_review` | madde sayısı `extract` çalışana kadar bilinmiyor, yani iş graf kurulurken yerleştirilemiyor |
| `JOIN` | `synthesis` | birleştirme *hangi* girdinin degrade olduğunu görmek zorunda; dict alan bir task eksik girdi ile boş girdiyi ayırt edemez |
| `COMPENSATE` | `retract` | bağımlılıklarla hiç planlanmıyor — bir hatayı bekliyor |

`archive`, side effect'ten *sonra* düşebilecek bir şey olsun diye var; saga'nın
var olma sebebi tam olarak bu durum: görüş gitti, ona eşlik etmesi gereken
kayıt yapılamadı ve alıcıya haber verilmesi gerekiyor.

## Bu iş neden tek ajanla yapılmadı

Beş tool'lu tek bir ajan daha kısa olurdu ve dört şeyi kaybederdi.

**Kısmi başarının duracağı bir yer yok.** Emsal kaynağı kapalıyken bir ajan ya
pes eder ya boşluğun etrafından sessizce yazar. Grafın bunun için bir durumu
var, onu yayıyor ve görüşün içine koyuyor.

**Yeniden koşular ilk koşu kadar pahalı.** Bir ajanın transcript'i adreslenebilir
iş değildir. Burada `(seed, node_id, fingerprint)`, değişmemiş bir işin yeniden
koşulmasının bedava olması demek.

**Bağımsız iş gerçekten bağımsız.** İki uzmanın ayrı tool registry'leri ve ayrı
context'leri var, yani hiçbiri diğerinin sonucuna ikna edilemiyor ve aynı
superstep'te koşuyorlar. Tek context'te ikisini de yapan bir ajan hem seri hem
çapraz bulaşmış olur.

**Side effect geri alınabilir.** E-posta göndermiş bir ajan onu geri alamaz,
çünkü göndermenin bir adım olduğunu hiçbir şey kaydetmemiştir.

!!! note "Dürüst karşı argüman"

    Üç adımlı ve side effect'i olmayan bir akış için bütün bunlar fazladan yük
    ve tek ajan doğrudur. Multi-agent çoğu zaman gereksizdir; değer, *burada*
    neden gerekli olduğunu söyleyebilmekte.

## State transition tablosu

Dokuz durum, ve `test_dag_execute.py` executor'ı bu tabloya karşı doğruluyor.
İkisi çeliştiğinde yalan söyleyen tablodur.

| Nereden | Nereye | Ne zaman |
|---|---|---|
| `PENDING` | `READY` | her bağımlılık terminal bir duruma ulaştı |
| `PENDING` | `SKIPPED` | bir `requires` bağımlılığı `FAILED` ya da `SKIPPED` |
| `PENDING` | `SKIPPED` | bir `GATE` bağımlılığı `passed: false` döndürdü |
| `READY` | `RUNNING` | scheduler bu superstep'te aldı |
| `RUNNING` | `SUCCEEDED` | döndü, her girdisi tam |
| `RUNNING` | `DEGRADED` | döndü ama bir `optional` girdi eksik ya da degrade — veya node kendini degrade ilan etti |
| `RUNNING` | `READY` | exception fırlattı veya timeout oldu, deneme hakkı var |
| `RUNNING` | `FAILED` | deneme hakkı bitti ya da node'un maliyet tavanı aşıldı |
| `READY` | `SKIPPED` | çalışmadan önce işin maliyet tavanına ulaşıldı |
| `SUCCEEDED` / `DEGRADED` / `FAILED` | `COMPENSATING` | aşağı akışta bir şey düştü ya da node'un kendisi düştü |
| `COMPENSATING` | `COMPENSATED` | telafi döndü |
| `COMPENSATING` | `FAILED` (iş `needs_human`) | telafi exception fırlattı. İkinci deneme yok. |

`SKIPPED` her raporda `FAILED`'dan ayrı sayılıyor. İkisini birleştirmek, işi
doğru bir kararla yapmayan bir koşuyu bozulmuş gibi gösterir.

## `requires` ve `optional` farklı kenarlar

```python
Node(
    "synthesis",
    NodeKind.JOIN,
    _synthesis,
    requires=("clause_review", "statute"),
    optional=("caselaw",),          # (1)
)
```

1.  Akıştaki en sonuç doğurucu tek satır.

Düşen bir sert bağımlılık kendine bağlı olanları da götürür — `SKIPPED`, çünkü
bir şey bozulmadı. Düşen bir yumuşak bağımlılık ise onları çalışabilir bırakır
ve `DEGRADED` işaretler; işaret aşağı akışa yayılır.

Bu ayrım olmadan iki kötü akıştan biri elde edilir: her şey zorunludur ve tek
bir tutarsız sorgu işi öldürür; ya da hiçbiri zorunlu değildir ve bir sentez
kanıtının yarısıyla vardığı sonucu sessizce rapor eder.

O `optional`, kodun içinde şunu söylüyor: mevzuata dayanan bir due diligence
görüşü bir çekinceyle teslim edilmeye değer, yalnızca emsale dayanan bir görüş
değer değildir. **Bu bir hukuk kararıdır, mühendislik kararı değil** ve bir
reviewer'ın görüp itiraz edebileceği yer olan grafa aittir.

## `DEGRADED` nerede görünür

Her yerde, sonuçtan önce. Durum yayılıyor, `missing_data_section` eksik bir şey
olsun olmasın basılıyor — açık bir "hiçbiri", olmayan bir bölümden farklı bir
ifadedir — ve önemli olan kısım: eksik girdi listesi log'a değil, sentezin
**içine** geçiyor:

```python
missing = list(context.missing_inputs) + list(context.degraded_inputs)
caveat = "" if not missing else (
    "\n\nINCOMPLETE: this opinion was written without " + ", ".join(missing)
    + ". Treat the conclusion as provisional in that respect."
)
```

Neyi kaçırdığı söylenmemiş bir sentez, her şeye sahip olan bir sentezin
özgüveniyle yazar; ve okuyucunun üzerine aksiyon aldığı şey o özgüvenli
paragraftır.

```python
def test_a_failed_optional_specialist_degrades_the_opinion():
    report = run_flow(caselaw_fails=True)
    opinion = report.records["synthesis"].result.output["opinion"]

    assert "INCOMPLETE" in opinion and "caselaw" in opinion
    assert len(sent_replies(SEED)) == 1     # degrade bir görüş yine de teslim edilir
```

## Fingerprint'e ne dahil

Bir fingerprint tek bir soruyu cevaplar: *bu node'u tekrar çalıştırsam aynı şeyi
mi üretirdi?* Cevabı değiştiren her şey içeri girer, geri kalan dışarıda kalır,
çünkü gereksiz her bileşen doğru olan bir cache hit'ini çöpe atar.

=== "Dahil"

    - node id'si ve `version`'ı — prompt dosyasının hash'inden türetiliyor, elle
      tutulmuyor. Birinin artırmayı unuttuğu bir integer, artık var olmayan bir
      prompt'un cevaplarını sunan bir cache demektir.
    - seed. İki belge hiçbir şey paylaşmaz.
    - her bağımlılığın çıktısı — sert ve yumuşak, sıralanmış hâlde.
    - varsa fan-out item'ı.

=== "Hariç"

    - duvar saati. Dahil etmek, cache'i hiç kullanmamakla aynı şey.
    - deneme numarası. Aynı işin retry'ı aynı iştir.
    - maliyet ve token sayıları — bunlar girdi değil *sonuç*.
    - ilgisiz node'ların durumu. Daha güvenli hissettirdiği için cazip, ve
      yanlış: dalları birbirine bağlar, herhangi bir yerdeki değişiklik her şeyi
      yeniden çalıştırır.
    - **yürütme politikası**: `max_attempts`, `timeout_s`, `cost_ceiling_usd`.

Sonuncusu ilginç olan karar ve gerekçesi somut: politika anahtarın içinde
olsaydı, tek bir tutarsız node'u geçirmek için bir timeout'u yükseltmek, zaten
başarılı olmuş pahalı node'lar dahil bütün grafı yeniden çalıştırırdı.

```python
def test_changing_one_node_version_reruns_it_and_its_subtree():
    ...
    assert calls == ["b", "c"], "a değişikliğin yukarısında ve yeniden çalışmamalı"
```

!!! danger "Başarıları cache'le, hataları asla"

    `ResultStore.put` rapor için her terminal durumu kaydediyor; `get` yalnızca
    `SUCCEEDED` ve `DEGRADED` döndürüyor. Cache'lenmiş geçici bir hata kalıcı
    bir hatadır.

## Telafi iki tetikleyiciyle çalışır

```python
def _needs_compensating(target_id, nodes, records) -> str:
    if records[target_id].state is NodeState.FAILED:
        return f"{target_id} failed after it may already have taken effect"
    ...
    failed_below = [n for n in _downstream_of(target_id, nodes) if failed(n)]
```

1. **Aşağı akışta bir şey düştü.** Side effect gerçekleşti ve parçası olduğu iş
   tamamlanmadı — klasik saga durumu.
2. **Node'un kendisi düştü.** Patlayan bir gönderim, gerçekleşmemiş bir gönderim
   değildir: mesaj gitmiş ve onayı kaybolmuş olabilir. Telafi idempotent olduğu
   için güvenli; hiçbir şey olmadığını varsaymak değil.

Bilinçli olarak tetikleyici **değil**: bu node'un ne bağlı olduğu ne de
beslediği, grafın başka bir yerindeki bir hata. Opsiyonel bir emsal
sorgusunun timeout olması, görüşün gönderilmiş olması gerekip gerekmediğiyle
ilgisizdir ve ilgisiz bir dal düştü diye onu geri çekmek kesintiden daha kötü
olurdu.

```python
def test_an_unrelated_failure_does_not_retract_a_good_delivery():
    report = run_flow(caselaw_fails=True)

    assert report.records["caselaw"].state is NodeState.FAILED
    assert report.records["retract"].state is NodeState.SKIPPED
    assert len(sent_replies(SEED)) == 1
```

Başarısız bir telafi yeniden denemek yerine yükseltiliyor. İki otomatik
kurtarma katmanı bir kötü durumu ikiye çıkarır ve ikincisi her zaman kimsenin
runbook'u olmayan durumdur: `needs_human`, çıkış kodu 3, operatör kuyruğu.

## Statik graf mı, supervisor mı?

**Akışın şekli sabitse graf, değilse supervisor.** Due diligence akışı her zaman
çıkarım yapıyor, her zaman maddeleri inceliyor ve her zaman iki uzmana da
soruyor — yani graf doğru cevap, ve `dagrun/supervisor.py` karşılaştırmayı
somutlaştırmak için var.

Supervisor on beş satır yönlendirme ve üç kısıt; her biri, olmadığında hemen
ortaya çıkan bir hata:

1. **Tamamlanan uzmanlar aday listesinden çıkıyor.** Aksi hâlde model aynı
   uzmanı sonsuza kadar ister, çünkü son cevabı işe yaramıştır.
2. **Kodda sert bir adım tavanı**, prompt'ta bir cümle değil. Kötü bir
   yönlendirme kararının sınırsız bir faturaya dönüşmesini engelleyen şey budur.
3. **Bilinmeyen bir ad, ilk bekleyen uzmana düşüyor**, exception fırlatmıyor.
   Halüsinasyon bir ada çöken bir supervisor, kurtarılabilir bir yönlendirme
   hatasını başarısız bir işe çevirir.

```python
def test_a_hallucinated_specialist_falls_back_rather_than_raising():
    out = build_supervisor(chooser=lambda state, outstanding: "notary-expert").invoke(...)

    assert out["visited"] == ["statute", "caselaw"]
```

`langgraph-supervisor` değil: hâlâ 0.0.x bandında ve o üç kısıt dosyanın bütün
değeri. Döngüye sahip olan bir bağımlılık kısıtlara da sahip olur.

Paralel uzmanlar tek bir state anahtarına yazıyor, yani reducer'ın sıradan
bağımsız olması gerekiyor — aşama 06'nın findings reducer'ıyla aynı kural, aynı
sebeple.

## Başka yerde karşılığı olmayan test

```python
@pytest.mark.parametrize(("node_id", "dropped"), [...])
def test_every_required_edge_is_real(node_id: str, dropped: str) -> None:
    """Bir sert bağımlılığı kaldır; node'un çıktısı değişmek zorunda."""
```

Cevabı değiştirmeyen bir `requires` kenarı bağımlılık değildir — paralel
çalışabilecek işin seri hâle getirilmesidir. Bunu bir kod tabanında başka
hiçbir şey yakalamaz: tip hatası değil, düşen bir test değil, kimsenin
gösterebileceği bir performans regresyonu değil. Test her seferinde bir sert
kenarı düşürüyor ve o kenarı tanımlayan node'un onsuz farklı bir sonuç
ürettiğini doğruluyor.

## Koşu raporunu okumak

```bash
uv run dagrun --seed SZL-2026-0431 --fail archive
```

```text
job SZL-2026-0431
  outcome        failed
  supersteps     7
  cost           $0.0780  (spent this run $0.0000)
  cache hits     19
  reason         completed

  node                 state         attempts  cost      cached  note
  --------------------------------------------------------------------
  archive              failed        1         $0.0000   no      RuntimeError: the source…
  deliver              compensated   1         $0.0000   yes     opinion delivered
  retract              compensated   1         $0.0000   no      archive failed downstream…
  ...
```

İki sütuna işaret etmeye değer. `cost` ve `spent this run` farklı sayılar:
ilki, sonucu üretmeye katkısı olan bütün koşular boyunca sonucun maliyeti;
ikincisi bu koşunun ödediği. Aradaki fark store'un kazandırdığı.

Çıkış kodu, bir shell'in dallanabileceği bir sayı: `0` temiz, `1` degrade, `2`
başarısız, `3` insan gerekiyor. Degrade olan bir iş, başarısız olan bir iş
değildir; ikisini aynı sayan bir pipeline ya çok sık ya hiç uyarmaz.

## Checklist

--8<-- "dag.tr.md"
