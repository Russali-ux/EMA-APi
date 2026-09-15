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

from ema_epi_client import EmaEpiClient, ORG_IDS, EmaEpiConfigError, filtrar_lists_por_agencia

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "ema_epi_data.json")

DEFAULT_AGENCIES = ["EMA", "AEMPS"]


def normalizar_texto(texto: str) -> str:
    texto = re.sub(r"\s+", " ", texto or "").strip().lower()
    return texto


def calcular_hash(secciones: list[dict]) -> str:
    payload = json.dumps(secciones, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extraer_texto_narrativo(div_html: str) -> str:
    return re.sub(r"<[^>]+>", " ", div_html or "")


def aplanar_secciones(secciones_raw: list[dict], nivel: int = 0) -> list[dict]:
    """Las secciones ePI son recursivas (section.section[]...). Las
    aplanamos en una lista ordenada de {titulo, texto, nivel} para
    poder hashear y mostrar diffs de forma estable."""
    resultado = []
    for section in secciones_raw or []:
        titulo = section.get("title", "")
        texto = ""
        if "text" in section and "div" in section["text"]:
            texto = extraer_texto_narrativo(section["text"]["div"])
        resultado.append(
            {"nivel": nivel, "titulo": titulo, "texto": normalizar_texto(texto)}
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
        "secciones": secciones,
        "contenido_hash": calcular_hash(secciones),
    }

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

    try:
        os.makedirs(DATA_DIR, exist_ok=True)

        print("Consultando el catálogo completo de PI Lists (GET /List)...")
        todas_las_lists = client.get_all_lists()
        print(f"{len(todas_las_lists)} PI Lists totales en el piloto")

        lists_filtradas = filtrar_lists_por_agencia(todas_las_lists, org_ids_pedidos)
        print(f"{len(lists_filtradas)} PI Lists coinciden con {args.agencies}")

        todos_los_documentos = []
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
                try:
                    doc = procesar_bundle(
                        client, bundle_id, org_id_real, agencia, item_extensions, display
                    )
                    if doc:
                        doc["dominio"] = dominio
                        todos_los_documentos.append(doc)
                except Exception as exc:  # noqa: BLE001
                    print(f"Error procesando bundle {bundle_id}: {exc}", file=sys.stderr)

            if i % 5 == 0:
                print(f"{i}/{len(lists_filtradas)} PI Lists procesadas...")

        output = {
            "generado_en": datetime.now(timezone.utc).isoformat(),
            "documentos": todos_los_documentos,
        }
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"Listo: {len(todos_los_documentos)} documentos guardados en {OUTPUT_PATH}")

    except EmaEpiConfigError as exc:
        print(f"ERROR DE CONFIGURACIÓN: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
