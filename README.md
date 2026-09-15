# EMA/AEMPS ePI Monitor

Monitorea cambios de contenido en documentos ePI (Ficha Técnica/SPC, Anexo II,
rotulado, Prospecto/PL) publicados a través de la **EMA.EPI.Consuming API**
(https://epi.developer.ema.europa.eu/api-details) para:

- **EMA (procedimiento centralizado)** — `ORG-100013412`
- **AEMPS, España** — `ORG-100003943`

Complementa (no reemplaza todavía) a la skill `cima-ft-prospecto`, que cubre
AEMPS vía scraping HTML de CIMA. Este repo usa la API FHIR oficial de la EMA,
que además cubre el procedimiento centralizado europeo.

## ✅ Fase 0 completada (15/09/2026) — hallazgos reales de la API

Se confirmó la base URL real contra la API en vivo (no solo la
documentación del portal, que es imprecisa en varios puntos):

- **Base URL:** `https://epi.ema.europa.eu/consuming/api/fhir`
- **Sin API key**: acceso público de lectura confirmado.
- **Rutas reales** (distintas de los nombres de operación del portal):
  `GET /Bundle/{id}` (no `/BundleById/{id}`), `GET /List/{id}`, `GET /List`,
  `GET /Bundle` para búsquedas.
- **El piloto es pequeño**: 23 PI Lists publicadas en total a la fecha
  (11 de ellas EMA + AEMPS). Con ese volumen, un cron diario es perfectamente
  viable sin riesgo de saturar la API.

Limitaciones de datos descubiertas (el scraper ya las maneja):

- El parámetro `regulatoryAgency` **no filtra server-side** — la API devuelve
  un `OperationOutcome` de warning y de todas formas retorna todas las Lists.
  El filtrado por agencia se hace client-side leyendo
  `List.subject.extension` (`regulatoryAgency`).
- `resourceType`, `language` y `status` del documento dentro del Bundle
  llegan **serializados como enteros (0)** en vez de strings — no se usan;
  el tipo real sale de `extension[documentType]` (con fallback a
  `type.coding[].display`) y el idioma del sufijo entre paréntesis de ese
  mismo display (ej. `"... (English)"`), con fallback al `item.display` de
  la PI List padre.
- Las secciones son **recursivas** (`section[].section[]...`), no una lista
  plana — el scraper las aplana con `aplanar_secciones()`.
- ~20% de los documentos de Lists más antiguas (formato pre-2025) no traen
  el idioma en ningún campo accesible de la API; quedan marcados como
  `"Desconocido"` — es una limitación real de esos datos, no del parser.

Todo esto ya está implementado y **probado en vivo**: la corrida de prueba
trajo 61 documentos reales (42 EMA + 19 AEMPS) sin errores.

## 📄 Hallazgos adicionales de la especificación oficial (EMA/286879/2021, v1.2)

El PDF "Electronic Product Information (ePI) Standard and API Specification v1"
aporta contexto que el portal `epi.developer.ema.europa.eu` no muestra, y se
verificó cada punto contra la API real:

- **Modelo de datos oficial**: cada ePI es un "Document Bundle" (Composition +
  Product List de `MedicinalProductDefinition` + Binaries), agrupado por una
  "PI List" (nuestro recurso `List`). Confirma que nuestro modelo
  List → Bundle → Composition es el correcto.
- **Extensiones documentadas mas NO pobladas en los datos actuales**: el spec
  define `documentType` y `language` como extensiones tipo `Coding` sobre
  `List.entry.item`, y `domain` sobre `List.subject`. Se probó contra la API
  real: `domain` **sí está poblado** (se usa, ver abajo), pero `documentType`
  y `language` sobre `item` **no están poblados** en ningún documento
  probado — de ahí que el scraper dependa del texto de `display`. El código
  ya intenta leer esas extensiones primero (por si EMA las empieza a poblar)
  y cae al parseo de texto si no están.
- **`domain` extension agregada**: cada documento ahora guarda `dominio`
  (hoy siempre `"Human Use"` — el piloto no tiene productos veterinarios
  todavía, pero si se agregan en el futuro ya quedan identificables sin
  reprocesar el histórico).
- **`_include=List:item` no funciona en este despliegue**: la especificación
  de FHIR search (hl7.org/fhir/search.html#content e `#include`) describe
  este mecanismo para traer los Bundles referenciados en la misma llamada
  que la List — se probó en vivo y el servidor lo ignora (devuelve solo las
  Lists). El scraper sigue haciendo un `GET /Bundle/{id}` por documento;
  no es un problema de rendimiento dado que el piloto tiene solo 23 Lists.
- **Convención de versionado `/v1/` no implementada**: el spec dice que
  cada endpoint debería llevar el prefijo `/v{version}` (ej. `GET /v1/Bundle`).
  El despliegue real no lo usa (`/consuming/api/fhir/Bundle/{id}` a secas) —
  vale la pena revisar este prefijo si EMA anuncia una v2 en el futuro.
- **Chained search y `_content`** (`GET /List?item:Bundle.composition.title:contains=...`,
  búsqueda de texto completo) existen en la spec pero no se usan en el
  scraper actual: no aportan nada sobre el flujo "traer todo y diffear por
  hash" que ya cubre el volumen completo del piloto en una sola pasada.

## Arquitectura

```
ListBySearchParameter(regulatoryAgency=ORG-XXX)  →  N PI Lists (uno por medicamento)
    ListById(list_id)                             →  M Bundle ids por medicamento
        BundleById(bundle_id)                      →  contenido íntegro (FHIR Bundle)
```

Cada `Bundle` es un `Composition` FHIR (uno por idioma) con `section[]` que
contiene el texto real (Ficha Técnica, Anexo II, rotulado o prospecto) más
imágenes embebidas como `Binary` en `contained[]`.

No existe endpoint de "cambios recientes": el scraper recorre todo el
catálogo de cada agencia periódicamente y detecta diferencias por hash de
contenido (mismo patrón que ya usa `cima-ft-prospecto` para AEMPS/CIMA).

## Estructura del repo

```
scripts/ema_epi_client.py        # cliente delgado de los 4 endpoints FHIR
scripts/scraper_ema_epi.py       # orquesta List → Bundle → extrae secciones → hash
scripts/ema_epi_sync_supabase.py # upsert a Supabase (ema_epi_documentos) + log de cambios (ema_epi_cambios)
sql/schema.sql                   # esquema Supabase (tablas + RLS)
.github/workflows/ema_epi_alertas.yml  # cron semanal (ajustar tras Fase 0)
ema-epi/index.html               # dashboard (mismo patrón que cima/pavs/aems/india)
```

## Supabase

Proyecto compartido con el resto de Alertas-analyzer:
`ggbnfdaxtsngsjssrwrl` (región `ca-central-1`).

Ejecutar `sql/schema.sql` en el SQL editor de Supabase antes del primer run.

## Variables de entorno / GitHub Secrets

| Variable | Descripción |
|---|---|
| `EMA_EPI_BASE_URL` | Base URL real del API (pendiente Fase 0) |
| `EMA_EPI_SUBSCRIPTION_KEY` | Opcional — solo si Azure APIM lo exige |
| `SUPABASE_URL` | `https://ggbnfdaxtsngsjssrwrl.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (solo en GitHub Secrets, nunca en el repo) |
