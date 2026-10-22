"""
Scraper principal del módulo EMA/AEMPS ePI.

Flujo (confirmado contra la API real en la Fase 0, 15/09/2026):
  1. Un solo GET /List?_count=1000&status=current trae TODAS las PI Lists
     publicadas (23 en total en el piloto), cada una con su propio
     entry[] de referencias a Bundle ya embebido.
  2. Se filtran client-side las Lists cuya regulatoryAgency (dentro de
     subject.extension) coincide con las agencias pedidas, porque el
     filtro server-side documentado no funciona.
  3. Por cada Bundle referenciado se hace GET /Bundle/{id} y se extrae
     el documento (tipo, idioma, secciones recursivas) para hashear.

Uso:
    python scraper_ema_epi.py                 # corrida normal -> data/ema_epi_data.json
    python scraper_ema_epi.py --agencies EMA   # limitar a una agencia
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

from ema_epi_client import (
    EmaEpiClient,
    EmaEpiConfigError,
    EmaEpiFuenteCaida,
    ORG_IDS,
    filtrar_lists_por_agencia,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "ema_epi_data.json")
ESTADO_PATH = os.path.join(DATA_DIR, "ema_epi_estado.json")

DEFAULT_AGENCIES = ["EMA", "AEMPS"]


def normalizar_texto(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto or "").strip().lower()
    return texto


CAMPOS_HASH = ("nivel", "titulo", "texto")

# EU/1/16/1102/001 -> centralizado. El presentación (/001) es opcional.
RE_NUMERO_EU = re.compile(r"\bEU/([12])/(\d{2})/(\d{3,4})(?:/(\d{3}))?\b")


def calcular_hash(secciones: list[dict]) -> str:
    """Hash estable: solo nivel/título/texto normalizado. Los campos
    auxiliares (codigo, html) no entran, para que un cambio de maquetación
    o de metadatos no se reporte como cambio de contenido."""
    nucleo = [{k: s.get(k) for k in CAMPOS_HASH} for s in secciones]
    payload = json.dumps(nucleo, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extraer_codigo_seccion(section: dict) -> str | None:
    """Código de sección (p. ej. '4.3' en el SPC, '2' en el prospecto):
    primero `code.coding[].code`, si no, la numeración al inicio del título.
    Es lo que permite mapear la sección al campo de CIMA (segmentador.js)."""
    for coding in (section.get("code") or {}).get("coding", []) or []:
        if coding.get("code"):
            return str(coding["code"])
    m = re.match(r"^\s*(\d+(?:\.\d+)*)\.?\s", section.get("title") or "")
    return m.group(1) if m else None


def recolectar_identificadores(nodo, encontrados: list[dict] | None = None) -> list[dict]:
    """Recorre el JSON FHIR completo y junta todos los `identifier`
    ({system, value}) sin duplicados. El piloto no garantiza dónde viene
    el número de autorización, así que se buscan en cualquier recurso."""
    if encontrados is None:
        encontrados = []
    if isinstance(nodo, dict):
        ident = nodo.get("identifier")
        for item in ident if isinstance(ident, list) else [ident] if isinstance(ident, dict) else []:
            par = {"system": item.get("system"), "value": item.get("value")}
            if par["value"] and par not in encontrados:
                encontrados.append(par)
        for valor in nodo.values():
            recolectar_identificadores(valor, encontrados)
    elif isinstance(nodo, list):
        for valor in nodo:
            recolectar_identificadores(valor, encontrados)
    return encontrados


def extraer_numeros_eu(*nodos) -> list[str]:
    """Números de autorización centralizada (EU/x/yy/nnnn[/ppp]) que
    aparezcan en cualquier parte de los JSON dados."""
    numeros = []
    for nodo in nodos:
        for m in RE_NUMERO_EU.finditer(json.dumps(nodo, ensure_ascii=False)):
            if m.group(0) not in numeros:
                numeros.append(m.group(0))
    return numeros


def extraer_texto_narrativo(div_html: str) -> str:
    return re.sub(r"<[^>]+>", " ", div_html or "")


def aplanar_secciones(secciones_raw: list[dict], nivel: int = 0) -> list[dict]:
    """Las secciones ePI son recursivas (section.section[]...). Las
    aplanamos en una lista ordenada de {titulo, texto, nivel} para
    poder hashear y mostrar diffs de forma estable."""
    resultado = []
    for section in secciones_raw or []:
        titulo = section.get("title", "")
        html = ""
        if "text" in section and "div" in section["text"]:
            html = section["text"]["div"] or ""
        resultado.append(
            {
                "nivel": nivel,
                "titulo": titulo,
                "texto": normalizar_texto(extraer_texto_narrativo(html)),
                "codigo": extraer_codigo_seccion(section),
                "html": html,
            }
        )
        # recursión sobre subsecciones
        resultado.extend(aplanar_secciones(section.get("section"), nivel + 1))
    return resultado


def extraer_tipo_documento(resource: dict, item_extensions: list[dict] | None = None, fallback_display: str = "") -> str:
    """Cadena de fallback para el tipo de documento (SmPC/Anexo II/Rotulado/Prospecto),
    de más a menos confiable:

    1. `documentType` extension documentada oficialmente en el spec v1.2 sobre
       `List.entry.item` (Coding: system/code/display). NO está poblada en los
       datos actuales del piloto, pero se deja como prioridad por si EMA
       empieza a rellenarla (forward-compatible).
    2. `documentType` extension encontrada en la práctica sobre el propio
       recurso del Bundle (valueReference.reference) — esta SÍ existe en
       los documentos más nuevos del piloto (2025 en adelante).
    3. `type.coding[].display` del propio Bundle, quitando el idioma entre
       paréntesis si lo trae.
    4. `item.display` de la PI List padre (mismo tratamiento).
    """
    for ext in item_extensions or []:
        if ext.get("url", "").endswith("/extension/documentType"):
            coding = ext.get("valueCoding", {})
            if coding.get("display"):
                return coding["display"]

    for ext in resource.get("extension") or []:
        if ext.get("url", "").endswith("/extension/documentType"):
            return ext.get("valueReference", {}).get("reference", "Desconocido")

    for fuente in [
        c.get("display", "") for c in resource.get("type", {}).get("coding", [])
    ] + [fallback_display]:
        if fuente:
            return re.sub(r"\s*\([^)]+\)\s*$", "", fuente).strip()
    return "Desconocido"


def extraer_idioma(resource: dict, item_extensions: list[dict] | None = None, fallback_display: str = "") -> str:
    """Cadena de fallback para el idioma, de más a menos confiable:

    1. `language` extension documentada oficialmente sobre `List.entry.item`
       (Coding: system/code/display, ej. display="German"). Tampoco está
       poblada actualmente, se deja por forward-compatibilidad.
    2. Sufijo entre paréntesis en `type.coding[].display` del propio Bundle
       (ej. "... (English)") — la fuente que sí funciona hoy.
    3. Mismo sufijo pero en el `item.display` de la PI List padre, para
       Bundles antiguos que no traen el paréntesis en su propio type.coding.
    """
    for ext in item_extensions or []:
        if ext.get("url", "").endswith("/extension/language"):
            coding = ext.get("valueCoding", {})
            if coding.get("display"):
                return coding["display"]

    fuentes = [c.get("display", "") for c in resource.get("type", {}).get("coding", [])]
    fuentes.append(fallback_display)
    for fuente in fuentes:
        m = re.search(r"\(([^)]+)\)\s*$", fuente or "")
        if m:
            return m.group(1)
    return "Desconocido"


def extraer_dominio(pi_list: dict) -> str:
    """Extensión `domain` documentada en el spec v1.2 sobre `List.subject`
    (ej. display="Human Use"). Hoy todo el piloto es de uso humano, pero se
    extrae y guarda por si en el futuro se agregan productos veterinarios,
    para poder filtrarlos sin tener que re-procesar el histórico."""
    for ext in pi_list.get("subject", {}).get("extension", []):
        if ext.get("url", "").endswith("/extension/domain"):
            return ext.get("valueCoding", {}).get("display", "Desconocido")
    return "Desconocido"


def procesar_bundle(
    client: EmaEpiClient,
    bundle_id: str,
    org_id: str,
    agencia: str,
    item_extensions: list[dict] | None = None,
    fallback_display: str = "",
    pi_list: dict | None = None,
) -> dict | None:
    """Devuelve el documento extraído de un Bundle (uno por Bundle,
    ya que en la práctica cada Bundle trae un único documento en
    entry[0], no varias Composition como sugería la guía FHIR genérica)."""
    bundle = client.bundle_by_id(bundle_id)
    entries = bundle.get("entry", [])
    if not entries:
        return None

    resource = entries[0].get("resource", {})
    secciones = aplanar_secciones(resource.get("section"))
    tipo_documento = extraer_tipo_documento(resource, item_extensions, fallback_display)
    idioma = extraer_idioma(resource, item_extensions, fallback_display)
    nombre_medicamento = resource.get("title", "")

    return {
        "org_id": org_id,
        "agencia": agencia,
        "bundle_id": bundle_id,
        "idioma": idioma,
        "tipo_documento": tipo_documento,
        "nombre_medicamento": nombre_medicamento,
        "pi_list_id": (pi_list or {}).get("id", ""),
        "secciones": secciones,
        "contenido_hash": calcular_hash(secciones),
        # Insumos para vincular con CIMA (ver ema_epi_vincular_cima.py)
        "identificadores": recolectar_identificadores([bundle, (pi_list or {}).get("subject")]),
        "numeros_eu": extraer_numeros_eu(bundle, (pi_list or {}).get("subject")),
    }

ESTADO_OK = "ok"
ESTADO_PARCIAL = "parcial"
ESTADO_FUENTE_CAIDA = "fuente_caida"


def guardar_estado(inicio: str, estado: str, agencias: list[str], error=None, **conteos):
    """Escribe data/ema_epi_estado.json: lo lee el sync para registrar la
    corrida en Supabase (ema_epi_corridas) aunque la fuente esté caída."""
    payload = {
        "inicio": inicio,
        "fin": datetime.now(timezone.utc).isoformat(),
        "estado": estado,
        "agencias": agencias,
        "http_status": getattr(error, "http_status", None),
        "detalle_error": str(error)[:500] if error else None,
        **conteos,
    }
    with open(ESTADO_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def aviso_github(mensaje: str):
    """Anotación ::warning:: visible en el resumen del run (sin ponerlo en rojo)."""
    print(f"::warning title=EMA ePI::{mensaje}")


def salida_github(**valores):
    ruta = os.environ.get("GITHUB_OUTPUT")
    if not ruta:
        return
    with open(ruta, "a", encoding="utf-8") as f:
        for k, v in valores.items():
            f.write(f"{k}={v}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agencies",
        nargs="+",
        choices=list(ORG_IDS.keys()),
        default=DEFAULT_AGENCIES,
    )
    args = parser.parse_args()

    client = EmaEpiClient()
    org_ids_pedidos = {ORG_IDS[a] for a in args.agencies}
    org_id_to_agencia = {ORG_IDS[a]: a for a in args.agencies}
    inicio = datetime.now(timezone.utc).isoformat()
    os.makedirs(DATA_DIR, exist_ok=True)

    try:
        print("Consultando el catálogo completo de PI Lists (GET /List)...")
        todas_las_lists = client.get_all_lists()
        print(f"{len(todas_las_lists)} PI Lists totales en el piloto")
    except EmaEpiFuenteCaida as exc:
        # La EMA está caída: no se toca el snapshot anterior, se deja
        # constancia y el job termina en verde (el sync solo registra la corrida).
        aviso_github(f"Fuente EMA ePI caída: {exc}")
        guardar_estado(inicio, ESTADO_FUENTE_CAIDA, args.agencies, error=exc)
        salida_github(fuente_ok="false")
        return
    except EmaEpiConfigError as exc:
        print(f"ERROR DE CONFIGURACIÓN: {exc}", file=sys.stderr)
        sys.exit(1)

    lists_filtradas = filtrar_lists_por_agencia(todas_las_lists, org_ids_pedidos)
    print(f"{len(lists_filtradas)} PI Lists coinciden con {args.agencies}")

    todos_los_documentos = []
    bundles_total = 0
    bundles_con_error = 0
    ultimo_error_fuente: EmaEpiFuenteCaida | None = None
    for i, pi_list in enumerate(lists_filtradas, start=1):
        # Determinar la agencia real de esta list a partir de su extension
        org_id_real = None
        for ext in pi_list.get("subject", {}).get("extension", []):
            if ext.get("url", "").endswith("/extension/regulatoryAgency"):
                org_id_real = ext.get("valueCoding", {}).get("code")
                break
        agencia = org_id_to_agencia.get(org_id_real, "DESCONOCIDA")
        dominio = extraer_dominio(pi_list)

        bundle_refs = [
            (
                entry["item"]["reference"].split("/")[-1],
                entry["item"].get("display", ""),
                entry["item"].get("extension", []),
            )
            for entry in pi_list.get("entry", [])
            if "item" in entry
        ]

        for bundle_id, display, item_extensions in bundle_refs:
            bundles_total += 1
            try:
                doc = procesar_bundle(
                    client, bundle_id, org_id_real, agencia, item_extensions, display,
                    pi_list=pi_list,
                )
                if doc:
                    doc["dominio"] = dominio
                    todos_los_documentos.append(doc)
            except EmaEpiFuenteCaida as exc:
                bundles_con_error += 1
                ultimo_error_fuente = exc
                print(f"Bundle {bundle_id}: fuente no disponible ({exc})", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                bundles_con_error += 1
                print(f"Error procesando bundle {bundle_id}: {exc}", file=sys.stderr)

        if i % 5 == 0:
            print(f"{i}/{len(lists_filtradas)} PI Lists procesadas...")

    if bundles_total and not todos_los_documentos and ultimo_error_fuente:
        # Respondió /List pero ningún Bundle: misma situación que una caída total.
        aviso_github(f"Fuente EMA ePI caída (Bundles): {ultimo_error_fuente}")
        guardar_estado(
            inicio, ESTADO_FUENTE_CAIDA, args.agencies, error=ultimo_error_fuente,
            bundles_total=bundles_total, bundles_con_error=bundles_con_error,
        )
        salida_github(fuente_ok="false")
        return

    output = {
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "documentos": todos_los_documentos,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    estado = ESTADO_PARCIAL if bundles_con_error else ESTADO_OK
    if estado == ESTADO_PARCIAL:
        aviso_github(f"{bundles_con_error}/{bundles_total} Bundles no se pudieron leer")
    guardar_estado(
        inicio, estado, args.agencies, error=ultimo_error_fuente,
        pi_lists=len(lists_filtradas), bundles_total=bundles_total,
        bundles_con_error=bundles_con_error, documentos=len(todos_los_documentos),
    )
    salida_github(fuente_ok="true")
    print(f"Listo: {len(todos_los_documentos)} documentos guardados en {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
