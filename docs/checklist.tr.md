# Proda çıkmadan checklist

Buradaki her madde, üç aşamadan birinin düzeltmek zorunda kaldığı bir hataya
dayanıyor. Maddeler onları üreten aşamaya göre gruplanmış; her blok kendi
bölümünün sonunda da duruyor.

Bu listede model kalitesiyle ilgili tek bir madde yok. Bunlar model yeterince
iyi olduktan *sonra* yaşanan hatalar — cuma günü, çoktan ölmüş bir süreçte
ortaya çıkanlar.

## Kalıcı state ve insan onayı

[Aşama 06](06-contract-graph.md)'dan. Bir insan için duraklayan ya da side
effect üreten her akış için geçerli.

--8<-- "durability.tr.md"

## Orkestratörleri karşılaştırmak

[Aşama 07](07-workflow-stacks.md)'den. Savunmayı düşündüğün her framework
değerlendirmesi için geçerli.

--8<-- "stacks.tr.md"

## Koordinasyon ve kısmi başarı

[Aşama 08](08-dag-orchestrator.md)'den. İçinden birden fazla yol geçen her
runner için geçerli.

--8<-- "dag.tr.md"

## Önce sorulacak üç soru

Yukarıdakilerden önce, geri kalan her şeyin şeklini belirleyen üç soru:

1. **Bir insan karar verirken state nerede duruyor?** Cevap "süreçte" ise
   human-in-the-loop desteği yoktur — demo vardır.
2. **Bu şu anda ölse ne olur?** "Exception fırlatsa" değil — öldürülse.
   Framework'ün yakaladığı her şeyi yakalayabilmesi için ortada yakalayacak bir
   süreç kalmış olması gerekir.
3. **Kısmi başarı raporda nasıl görünüyor?** Cevap "başarı" ya da "başarısızlık"
   ise, rapor ikisinden biri hakkında yalan söylüyor.
