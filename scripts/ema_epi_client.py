"""
Cliente delgado para la EMA.EPI.Consuming API (FHIR).

✅ FASE 0 COMPLETADA (15/09/2026) — confirmado contra la API real:
  - Base URL: https://epi.ema.europa.eu/consuming/api/fhir
  - No requiere API key ni suscripción (acceso público de solo lectura)
  - Rutas reales:
      GET /Bundle/{id}   (no /BundleById/{id} como sugiere el nombre del operationId)
      GET /List/{id}
      GET /List?<params> (búsqueda)
      GET /Bundle?<params> (búsqueda)

  ⚠️ El parámetro `regulatoryAgency` documentado en el portal NO filtra
  server-side: la API devuelve un OperationOutcome de warning
  ("search parameter 'regulatoryAgency' is not supported for resource
  type 'List'") y de todas formas retorna TODAS las Lists publicadas
  (23 en total al momento de esta prueba — el piloto es pequeño).
  Por eso el filtrado por agencia se hace client-side, leyendo
  `List.subject.extension` (url=".../extension/regulatoryAgency",
  valueCoding.code).

  ⚠️ La respuesta de `/List` con `_count` alto ya trae las Lists
  completas embebidas (cada una con su propio `entry[]` de referencias
  a Bundle) — no hace falta un ListById por cada una en el flujo normal.

  ⚠️ Bug de serialización del pilotos: en el Bundle de documento,
  `resourceType`, `language` y `status` del recurso interno llegan
  como enteros (0) en vez de strings ("Composition", "en", "final").
  El scraper NO depende de esos campos: usa `extension[documentType]`
  y `type.coding[].display` (que trae el idioma entre paréntesis,
  ej. "Summary of Product Characteristics (English)").

  ⚠️ Las secciones son recursivas (`section[].section[]...`), no una
  lista plana — ver `extraer_secciones` en scraper_ema_epi.py.
"""

import os
import time
import requests

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

# ORG IDs de las agencias participantes en el piloto ePI (confirmados en
# epi.developer.ema.europa.eu/product)
ORG_IDS = {
    "EMA": "ORG-100013412",
    "AEMPS": "ORG-100003943",
    "DKMA": "ORG-100003918",
    "MEB": "ORG-100003934",
    "MPA": "ORG-100003944",
}


class EmaEpiConfigError(RuntimeError):
    """La configuración de la Fase 0 (base URL / key) no está lista."""


class EmaEpiClient:
    def __init__(self, base_url: str | None = None, subscription_key: str | None = None):
        self.base_url = base_url or os.environ.get("EMA_EPI_BASE_URL", "").rstrip("/")
        self.subscription_key = subscription_key or os.environ.get(
            "EMA_EPI_SUBSCRIPTION_KEY"
        )
        self.session = requests.Session()
        if self.subscription_key:
            self.session.headers["Ocp-Apim-Subscription-Key"] = self.subscription_key

    def _require_base_url(self):
        if not self.base_url:
            raise EmaEpiConfigError(
                "EMA_EPI_BASE_URL no está configurada. Completa la Fase 0 "
                "descrita en README.md antes de ejecutar el scraper."
            )

    def _get(self, path: str, params: dict | None = None) -> dict:
        self._require_base_url()
        url = f"{self.base_url}{path}"
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise last_exc

    def get_all_lists(self, count: int = 1000) -> list[dict]:
        """Trae TODAS las PI Lists publicadas en una sola llamada (recurso List).

        El piloto es pequeño (23 Lists totales a sept/2026), así que no hace
        falta paginar en la práctica; se deja `count` alto por seguridad.
        El filtro por agencia se hace después, client-side, con
        `filtrar_lists_por_agencia`.
        """
        data = self._get(
            "/List", params={"_count": count, "status": "current"}
        )
        listas = []
        for entry in data.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "List":
                listas.append(resource)
        return listas

    def list_by_id(self, pi_list_id: str) -> dict:
        """Devuelve una PI List puntual por id (uso puntual/depuración;
        el flujo normal usa get_all_lists)."""
        return self._get(f"/List/{pi_list_id}")

    def bundle_by_id(self, bundle_id: str) -> dict:
        """Devuelve el contenido íntegro del documento (FHIR Bundle).

        Ruta real confirmada: GET /Bundle/{id} (no /BundleById/{id}).
        """
        return self._get(f"/Bundle/{bundle_id}")

    def bundle_by_search_parameter(
        self,
        pms_id: str | None = None,
        data_carrier_identifier: str | None = None,
        language_code: str | None = None,
    ) -> dict:
        params = {}
        if pms_id:
            params["pmsId"] = pms_id
        if data_carrier_identifier:
            params["dataCarrierIdentifier"] = data_carrier_identifier
        if language_code:
            params["languageCode"] = language_code
        if not params:
            raise ValueError("Se requiere al menos un parámetro de búsqueda")
        return self._get("/Bundle", params=params)


def filtrar_lists_por_agencia(listas: list[dict], org_ids: set[str]) -> list[dict]:
    """Filtra client-side las PI Lists cuyo `subject.extension`
    (regulatoryAgency) coincide con alguno de los org_ids dados.

    Necesario porque el parámetro `regulatoryAgency` de la API no
    filtra server-side (ver nota de Fase 0 arriba).
    """
    filtradas = []
    for lst in listas:
        extensions = lst.get("subject", {}).get("extension", [])
        for ext in extensions:
            if not ext.get("url", "").endswith("/extension/regulatoryAgency"):
                continue
            code = ext.get("valueCoding", {}).get("code")
            if code in org_ids:
                filtradas.append(lst)
            break
    return filtradas
