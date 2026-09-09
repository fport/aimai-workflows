# 7. Tek akış, beş stack

Aynı destek akışı beş kez yazıldı — saf Python, LangGraph, pydantic-ai, OpenAI
Agents SDK, Strands Agents — ki "hangi framework" sorusu bir görüş olmaktan
çıkıp bir tabloya dönüşsün.

Bir talebi sınıflandır, bilgi tabanından madde getir, taslak cevap üret, talep
riskliyse insana dur, gönder. Beş adım, ve dördüncüsü bütün karşılaştırmayı
taşıyor: 1-3 ve 5 her framework'te neredeyse aynı görünüyor; akışın ortasında
durup state'i kalıcı hâle getirip saatler sonra bir insan kararıyla devam etmek
her birinde tamamen farklı görünüyor.

!!! done "Burada ne kurduk"

    İş mantığını tutan tek bir `core.py`, onu import eden beş orkestrasyon
    modülü, beşinin de geçtiği tek bir parametrize test paketi, 50 talep
    üzerinde bir benchmark ve her stack'in worker'ını iki ayrı noktada öldüren
    bir chaos script'i.

## Bunu karşılaştırma yapan kural

**İş mantığı bir kez yazılıyor.** `stacks/core.py` sınıflandırmayı, retrieval'ı,
taslak üretimini, risk kuralını ve gönderimi tutuyor; beş stack de onu import
ediyor. Beş dosya arasındaki fark orkestrasyon farkı ve başka hiçbir şey değil.

İş mantığı beş kez yazılsaydı benchmark'taki her sayı, aynı fikrin beş hafif
farklı implementasyonu tarafından bulandırılırdı — internetteki çoğu framework
karşılaştırmasının farkında olmadan ölçtüğü şey budur.

Söylenmeye değer iki sonuç:

- **Hiçbir prompt framework'ün alanlarının içinde durmuyor.** İki prompt da
  `prompts/` altında dosya ve aimai-kit'in registry'si üzerinden yükleniyor.
  System prompt'u bir decorator argümanında isteyen bir framework, render
  edilmiş metni alıyor. Lock-in'in pratik ölçüsü budur.
- **Risk eşiği `core` içinde tek bir sabit.** Her stack riski kendi yöntemiyle
  karar verseydi, benchmark'ın "onaya düşen" sütunu beş orkestratörü değil beş
  kuralı karşılaştırırdı.

## Beş sürüm, kendi ifadeleriyle

=== "saf Python"

    Orkestrasyon, SQLite'taki bir `step` kolonu üzerinde bir `match`.

    ```python
    def run(self, ticket_id: str) -> RunOutcome:
        step, payload = self._read(ticket_id) or ("", {})
        if step == "drafted":
            return self._after_draft(ticket, payload, resumed=True)
        if step == "classified":
            classification = Classification.model_validate(payload)
        else:
            classification = classify(self.client, ticket, registry=self.registry)
            self._write(ticket_id, "classified", classification.model_dump())
        ...
    ```

    Gösterdiği şey, akış düz bir çizgiyken kalıcı, resume edilebilir ve
    human-in-the-loop bir akışın ne kadar az şeye ihtiyaç duyduğu: step kolonu
    olan bir tablo, adım başına bir transaction ve idempotent bir side effect.

    Maliyeti de görünür. Resume yolu elle yazılmış, adım başına bir branch — ve
    o on bir satır, biri bir adım ekleyip onları güncellemeyi unuttuğunda
    bayatlayacak olan satırlar. History yok: talep başına tek satır ve
    "insan düzeltmeden önce sınıflandırma ne demişti" sorusunun cevabı yok.

=== "LangGraph"

    State machine framework'e devrediliyor.

    ```python
    def approval_gate(state: SupportState) -> SupportState:
        answer = interrupt({"ticket_id": state["ticket_id"], "question": "Send this reply?"})
        return {"decision": answer if answer in ("approve", "reject") else "reject"}

    graph.add_conditional_edges("compose", needs_approval, {...})
    ```

    Step kolonunu, `match`'i ve "neredeydim" mantığını yazmayı bırakıyorsun.
    Resume `invoke(None, config)` — adım başına branch yok, akışla senkron
    tutulacak bir resume yolu yok. `get_state_history(config)` bütün ara
    durumları bedavaya veriyor.

    Karşılığında kendi sürüm takvimi olan bir bağımlılık, serileştirilebilir
    olmak zorunda olan bir state şeması ve onu debug edecek herkesin önce
    öğrenmesi gereken bir zihinsel model — superstep, channel, reducer, replay
    — ödüyorsun.

=== "pydantic-ai"

    Tipli tool'lar ve *tool seviyesinde* bir human-in-the-loop.

    ```python
    @agent.tool
    def deliver(ctx: RunContext[SupportDeps], ticket_id: str) -> str:
        if is_risky(ticket, classification) and not ctx.tool_call_approved:
            raise ApprovalRequired
        return send_reply(ticket_id, ctx.deps.draft["text"], stack=NAME).idempotency_key
    ```

    Koşu bir cevap yerine `DeferredToolRequests` ile bitiyor ve sonraki koşu
    `DeferredToolResults(approvals={call_id: True})` ile devam ediyor.

    **State'i sen taşıyorsun.** Checkpointer yok; iki fiil arasında hayatta
    kalması gereken şey mesaj geçmişi ve bu dosya onu
    `ModelMessagesTypeAdapter` ile kendi seçtiği bir kolona serileştiriyor.
    LangGraph'tan daha fazla kod, karşılığında state'in nerede duracağı
    konusunda hiçbir görüş yok.

    **Gate tool'un içine taşınıyor.** İki graf sürümünde risk kontrolü side
    effect'ten *önce* bir branch. Burada onu gerçekleştiren tool'un *içinde* bir
    koşul, çünkü framework'ün onay mekanizması orada. Benimsemeden önce bilmeye
    değer: side effect'ten iki adım önce olmasını istediğin bir onayın ayrı bir
    tool olarak modellenmesi gerekir.

=== "OpenAI Agents SDK"

    Kontrolün en çoğu modelde. `needs_approval` bir callable kabul ediyor, yani
    paylaşılan risk kuralı çağrı başına karar verebiliyor:

    ```python
    async def approval_needed(ctx, params, call_id) -> bool:
        return is_risky(load_ticket(params["ticket_id"]), classification)

    @function_tool(needs_approval=approval_needed)
    def deliver(ticket_id: str) -> str: ...
    ```

    State bir `RunState`: dışarı `to_json()`, geri `RunState.from_json(agent,
    blob)`. Blob item'ları, kullanımı, onayları ve tool-use tracker'ı tutuyor —
    bütün koşuyu, ki SDK döngüyü senin yerine yeniden kurabilsin.

    İki operasyonel not: `from_json` async ama `Runner.run_sync` değil, yani
    senkron bir CLI köprü kurmak zorunda; ve **trace varsayılan olarak OpenAI'a
    gidiyor**. Bu depo import anında `set_tracing_disabled(True)` çağırıyor,
    bilinçli ve görünür şekilde, çünkü koşularını sessizce bir sağlayıcıya
    yükleyen bir karşılaştırma reposu bir şeyi ölçüp başka bir şey yapıyordur.

=== "Strands Agents"

    Deseni bozan sürüm: **sıraya model karar veriyor, state'i framework
    tutuyor.**

    ```python
    @tool(context=True)
    def deliver(ticket_id: str, tool_context: ToolContext) -> str:
        """Taslak cevabı müşteriye gönder."""
        if is_risky(ticket, classification):
            decision = tool_context.interrupt(
                name="approve_send",
                reason={"ticket_id": ticket_id, "question": "Send this reply?"},
            )
            if decision != "approve":
                return "rejected by the reviewer"
        return send_reply(ticket_id, draft["text"], stack=NAME).idempotency_key
    ```

    Koşu `stop_reason="interrupt"` ile bitiyor, session manager bekleyen tool
    yürütmesini diske yazıyor ve resume birinci sınıf bir girdi:
    `agent([{"interruptResponse": {"interruptId": …, "response": "approve"}}])`.

    **Aynı session ile kurulan yepyeni bir agent zaten duraklamış hâlde
    geliyor.** "State'i yeniden kurabilirsin" değil — `Agent` inşa edilirken
    geri yükleniyor. pydantic-ai ve Agents SDK'nın sana bıraktığı şey bu, ve bu
    sürümün koşu ortasındaki bir kill'den sonra *hiçbir şeyi* tekrarlamamasının
    sebebi de bu; diğer her sürüm en az bir çağrı tekrarlıyor.

    Tool'un içinde LangGraph'takiyle aynı kural geçerli: tool ilk satırından
    itibaren yeniden çalışıyor, bu yüzden gönderim `interrupt()` çağrısının
    altında duruyor.

    Session bir JSON dizini — `session.json`, `agent.json`, mesaj başına bir
    dosya — yani duraklamış bir koşu olay anında bir insan tarafından
    okunabiliyor. Mesaj geçmişinden büyük, `RunState` blob'undan çok küçük ve
    üçü arasında gece 3'te açmak isteyeceğin tek format.

## Benchmark

50 sentetik talep, aynı sıra, aynı deterministik model, her stack için üç geçiş:
koş, yeni bir instance'tan onayla, sonra hepsini tekrar koş ki bir retry'ın
hiçbir şeyi iki kez göndermediği görülsün.

| Stack | tamamlanan | onaya düşen | restart sonrası devam | çift gönderim | llm çağrısı | orkestrasyon satırı |
|---|---|---|---|---|---|---|
| saf Python | 50 | 15 | 15 | 0 | 100 | 160 |
| LangGraph | 50 | 15 | 15 | 0 | 100 | **151** |
| pydantic-ai | 50 | 15 | 15 | 0 | **300** | 259 |
| OpenAI Agents SDK | 50 | 15 | 15 | 0 | **300** | 255 |
| Strands Agents | 50 | 15 | 15 | 0 | **300** | 255 |

Açıkça söylenmeye değer iki bulgu.

**Üç katı model çağrısı.** Üç agent sürümü de, iş mantığının yaptığı iki çağrının
üstüne tool çağrısı başına bir model turn'ü harcıyor — model sırada ne
yapılacağına karar veriyor ve o karar bir istek. Şekli hiç değişmeyen beş
adımlık bir akış için 50 talep başına 200 fazla çağrı hiçbir şey satın almadı.
Şekil *gerçekten* değiştiği anda bir şey satın alıyorlar; o da
[aşama 08](08-dag-orchestrator.md)'in sorusu.

**LangGraph, elle yazmaktan daha fazla kod değil.** 151'e karşı 160 satır. Saf
sürüm satırlarını step kolonuna, `match`'e ve elle yazılmış resume yoluna
harcıyor — bir checkpointer'ın yazmış olacağı koda.

!!! note "Sayaçlar neden outbox'tan geliyor"

    `tamamlanan` ve `çift gönderim` mesaj deposundan hesaplanıyor, bir stack'in
    kendisi hakkında söylediğinden değil. Özellikle çift gönderim, müşterinin
    alacağı fazla satır sayısıdır ve önemli olan tek tanım budur.

## Chaos: iki noktada `SIGKILL`

| Stack | Gate'te öldürüldü → onaylandı mı? | Duraklamış state | Koşu ortasında öldürüldü → devam? | Tekrarlanan model çağrısı |
|---|---|---|---|---|
| saf Python | evet | 461 B | evet | 1 — yarım kalan adımdan devam |
| LangGraph | evet | 4.966 B | evet | 1 — yarım kalan adımdan devam |
| pydantic-ai | evet | 4.364 B | evet | 2 — koşu ortası state yok; baştan başladı |
| OpenAI Agents SDK | evet | 11.427 B | evet | 2 — koşu ortası state yok; baştan başladı |
| Strands Agents | evet | 5.996 B | evet | **0 — hiçbir şey tekrarlanmadı** |

Beşi de **gate'te duraklarken** öldürülmeyi atlatıyor ve hiçbiri geri
döndüğünde çift cevap göndermiyor. Çıta bu ve beşi de aşıyor.

**Koşu ortasında** öldürülmede ayrışıyorlar — ve iki değil üç gruba.

Adım başına state yazan iki sürüm yarım kalan adımdan devam ediyor, bir çağrı
tekrarlıyor. pydantic-ai ve Agents SDK'nın "koşu başladı" ile "koşu döndü"
arasında hiçbir şeyi yok, bu yüzden işi tekrarlıyorlar. Strands hiçbir şeyi
tekrarlamıyor: session manager her tool sonucunu düştüğü anda kalıcılaştırıyor,
yani yeniden başlayan worker sınıflandırma *ve* taslak hazır hâlde geliyor —
iki graf sürümünün ulaştığından daha ince bir granülarite, ve burada yazılmış
bir şey değil SDK'nın varsayılanı.

!!! warning "\"Resume ediyor mu\" sorusunun dürüst cevabı"

    pydantic-ai ve Agents SDK koşu ortasında checkpoint alabiliyor — sırasıyla
    `agent.iter()` ve turn başına `to_state()` ile. Hiçbiri bunu senin yerine
    yapmıyor ve bu depo da yazmadı. Yani o ikisi için: evet, yazmaya razı
    olduğun granülaritede. Strands için cevap koşulsuz, ki mesele de bu.

## Karar tablosu — maliyet ve kontrol

| Stack | Orkestrasyon satırı | Sıradaki adıma kim karar veriyor | Şu durumda tercih et |
|---|---|---|---|
| saf Python | 160 | tamamen sen | akış düz bir çizgi, ekip küçük ve bir bağımlılık eklemek gerekçe istiyorsa. Akış değiştirilmekten çok okunacaksa da doğru cevap budur. |
| LangGraph | 151 | sen, bir topolojide | akış dallanıyor, insanlar için duruyor ya da sonradan incelenebilir olması gerekiyorsa. Onu debug edecek herkesin öğrenmesi gereken bir zihinsel modele mal olur. |
| pydantic-ai | 259 | tipli tool'lar içinde model | kod tabanı zaten pydantic şeklindeyse ve bir runtime benimsemeden tipli tool istiyorsan. State'i sen taşırsın. |
| OpenAI Agents SDK | 255 | çoğunlukla model | iş gerçekten girdiye göre değişiyorsa ve zaten OpenAI üzerindeysen. Döngü sağlayıcının ve state blob'u taşınmaz. |
| Strands Agents | 255 | çoğunlukla model | iş girdiye göre değişiyorsa **ve** duraklamanın kalıcılığını kendin yazmadan istiyorsan. |

## Karar tablosu — state, HITL ve lock-in

| Stack | State nerede | HITL mekanizması | Lock-in |
|---|---|---|---|
| saf Python | senin tasarladığın bir `runs` tablosu | `approve`'un okuduğu bir `step` kolonu | yok; senin şeman, senin SQL'in |
| LangGraph | checkpointer (SQLite/Postgres) | `interrupt()` + `Command(resume=…)` | orta: state şeması ve topoloji LangGraph'ın, node'lar değil |
| pydantic-ai | **senin** serileştirdiğin mesaj geçmişi | `ApprovalRequired` + `DeferredToolResults` | düşük: istediğin yerde tuttuğun JSON |
| OpenAI Agents SDK | **senin** serileştirdiğin `RunState` blob'u | `needs_approval` + `state.approve()` | en yüksek: blob SDK'nın şeklinde. Okunabilir, taşınabilir değil. |
| Strands Agents | **SDK'nın yazdığı** bir session | `tool_context.interrupt()` + `interruptResponse` girdisi | orta: format SDK'nın ama dosya sisteminde ya da S3'te düz JSON |

**Trace nereye gidiyor** — bu, sorun olana kadar kimsenin okumadığı bir konu:
saf Python'da hiç yok. LangGraph `LANGCHAIN_TRACING_V2` set edilmişse
LangSmith'e gönderiyor, değilse hiçbir yere. pydantic-ai OpenTelemetry
yayıyor, yapılandırılmadıkça kapalı. Agents SDK varsayılan olarak OpenAI'a
gönderiyor. Strands OpenTelemetry yayıyor ve `callback_handler=None`
verilmedikçe stdout'a yazıyor — notebook'ta zararsız, ama sürecin çıktısını
parse eden her çağıranı bozar.

## Bugün üretimde hangisini koşardım

Bu şekildeki bir akış için **LangGraph**. State machine'i elle yazmakla aynı
satır sayısına mal oluyor ve karşılığında resume yolu, akışla senkron tutmak
zorunda olduğum bir kod olmuyor. Diğer yarısı state history: bir reviewer ilk
kez "ben düzeltmeden önce ne demişti" diye sorduğunda saf sürümün cevabı yok.

*Burada* bir agent SDK'sına uzanmazdım. Akışın şekli değişmiyor; modelin her
talepte o şekli yeniden keşfetmesi için üç katı model çağrısı ödemek, varyans
eklemek için para harcamaktır.

**Şekil gerçekten değiştiğinde Strands'e uzanırdım**, ve sebep benchmark değil
chaos tablosu. Üç agent SDK'sı model çağrısında aynı, orkestrasyon satırında
birbirinden dört satır uzakta; ayrıştıkları yer süreç öldüğünde ne olduğu.
İkisi duraklamış state'i saklamam, geri vermem ve kendi şemamla senkron tutmam
için bana veriyor — yazmam, test etmem ve yedeklemem gereken bir kalıcılık
katmanı. Strands onu kalıcılaştırıyor, geri yüklüyor ve adım sınırının altından
devam ediyor. Bu, "human-in-the-loop destekleniyor" ile "dayanıklı kısmı
yazdıktan sonra human-in-the-loop destekleniyor" arasındaki fark.

Fikrimi değiştirecek kısıt, ekibin zaten diğerlerinden birinde akıcı olması
olurdu. Beşi de bu depodaki her testi geçti; yukarıdaki farkların hiçbiri bir
ekosistemi yeniden öğrenmeye değmez.

## Checklist

--8<-- "stacks.tr.md"
