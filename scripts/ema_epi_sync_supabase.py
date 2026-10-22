"""
Sincroniza la salida de scraper_ema_epi.py con Supabase.

  - Registra SIEMPRE la corrida en ema_epi_corridas (también cuando la
    fuente EMA está caída), a partir de data/ema_epi_estado.json.
  - Si hubo datos: upsert en ema_epi_documentos (estado actual,
    dedupe_key = bundle_id + idioma) e insert en ema_epi_cambios cuando el
    hash difiere del guardado, con un resumen de las secciones afectadas.

Este repo es independiente de Alertas-analyzer: solo escribe en las tablas
ema_epi_* (y la puente ema_epi_cima_vinculo, desde ema_epi_vincular_cima.py).
Las tablas de CIMA (documentos_segmentados, etc.) no se tocan desde aquí.

Requiere las variables de entorno:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DATA_PATH = os.path.join(DATA_DIR, "ema_epi_data.json")
ESTADO_PATH = os.path.join(DATA_DIR, "ema_epi_estado.json")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

MAX_SECCIONES_EN_RESUMEN = 8


def supabase_headers(prefer: str = "return=minimal") -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def rest(method: str, tabla: str, **kwargs) -> requests.Response:
    resp = requests.request(
        method, f"{SUPABASE_URL}/rest/v1/{tabla}", timeout=30, **kwargs
    )
    if resp.status_code >= 400:
        print(f"Supabase {method} {tabla} -> {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
    resp.raise_for_status()
    return resp


def get_documento_actual(dedupe_key: str) -> dict | None:
    rows = rest(
        "GET",
        "ema_epi_documentos",
        headers=supabase_headers(),
        params={
            "dedupe_key": f"eq.{dedupe_key}",
            "select": "contenido_hash,contenido_secciones",
        },
    ).json()
    return rows[0] if rows else None


def upsert_documento(doc: dict, dedupe_key: str, contenido_cambio: bool = True):
    payload = {
        "org_id": doc["org_id"],
        "agencia": doc["agencia"],
        "pi_list_id": doc.get("pi_list_id") or "",
        "bundle_id": doc["bundle_id"],
        "nombre_medicamento": doc.get("nombre_medicamento"),
        "tipo_documento": doc.get("tipo_documento"),
        "idioma": doc.get("idioma"),
        "contenido_hash": doc["contenido_hash"],
        "contenido_secciones": doc.get("secciones"),
        "identificadores": doc.get("identificadores") or [],
        "numeros_eu": doc.get("numeros_eu") or [],
        "dominio": doc.get("dominio"),
        "fecha_ultima_actualizacion": datetime.now(timezone.utc).isoformat(),
        "dedupe_key": dedupe_key,
    }
    if not contenido_cambio:
        # Sin cambio de contenido: no se mueve la fecha de última actualización
        # (PostgREST solo actualiza las columnas presentes en el payload).
        payload.pop("fecha_ultima_actualizacion")
    rest(
        "POST",
        "ema_epi_documentos",
        headers=supabase_headers("resolution=merge-duplicates,return=minimal"),
        params={"on_conflict": "dedupe_key"},
        json=payload,
    )


def resumir_cambio(anteriores: list[dict] | None, nuevas: list[dict]) -> str | None:
    """Lista las secciones agregadas / eliminadas / modificadas, por título,
    para que el dashboard muestre qué cambió sin tener que abrir el diff."""
    if anteriores is None:
        return None

    def indice(secciones):
        out = {}
        for s in secciones or []:
            clave = s.get("codigo") or s.get("titulo") or ""
            out.setdefault(clave, s)
        return out

    antes, despues = indice(anteriores), indice(nuevas)
    agregadas = [k for k in despues if k not in antes]
    eliminadas = [k for k in antes if k not in despues]
    modificadas = [
        k for k in despues
        if k in antes and antes[k].get("texto") != despues[k].get("texto")
    ]

    def fmt(etiqueta, claves):
        if not claves:
            return None
        titulos = [
            (despues.get(k) or antes.get(k) or {}).get("titulo") or k
            for k in claves[:MAX_SECCIONES_EN_RESUMEN]
        ]
        extra = len(claves) - MAX_SECCIONES_EN_RESUMEN
        return f"{etiqueta} ({len(claves)}): " + "; ".join(titulos) + (f"; +{extra} más" if extra > 0 else "")

    partes = [p for p in (
        fmt("Modificadas", modificadas),
        fmt("Agregadas", agregadas),
        fmt("Eliminadas", eliminadas),
    ) if p]
    return " | ".join(partes) or "Cambio de estructura sin diferencias de texto por sección"


def registrar_cambio(doc: dict, actual: dict | None):
    payload = {
        "bundle_id": doc["bundle_id"],
        "nombre_medicamento": doc.get("nombre_medicamento"),
        "tipo_documento": doc.get("tipo_documento"),
        "agencia": doc["agencia"],
        "idioma": doc.get("idioma"),
        "tipo_cambio": "nuevo_documento" if actual is None else "contenido_modificado",
        "hash_anterior": actual["contenido_hash"] if actual else None,
        "hash_nuevo": doc["contenido_hash"],
        "resumen_cambio": resumir_cambio(
            actual.get("contenido_secciones") if actual else None, doc.get("secciones") or []
        ),
    }
    rest("POST", "ema_epi_cambios", headers=supabase_headers(), json=payload)


def registrar_corrida(estado: dict, conteos: dict):
    payload = {
        "inicio": estado.get("inicio"),
        "fin": datetime.now(timezone.utc).isoformat(),
        "estado": estado.get("estado", "desconocido"),
        "agencias": estado.get("agencias") or [],
        "http_status": estado.get("http_status"),
        "detalle_error": estado.get("detalle_error"),
        "pi_lists": estado.get("pi_lists"),
        "bundles_total": estado.get("bundles_total"),
        "bundles_con_error": estado.get("bundles_con_error"),
        "documentos": estado.get("documentos"),
        "nuevos": conteos.get("nuevos"),
        "modificados": conteos.get("modificados"),
        "sin_cambios": conteos.get("sin_cambios"),
        "github_run_url": github_run_url(),
    }
    rest("POST", "ema_epi_corridas", headers=supabase_headers(), json=payload)


def github_run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def leer_json(ruta: str) -> dict | None:
    if not os.path.exists(ruta):
        return None
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: configura SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        sys.exit(1)

    estado = leer_json(ESTADO_PATH) or {"estado": "desconocido"}

    if estado.get("estado") == "fuente_caida":
        registrar_corrida(estado, {})
        print(f"Fuente EMA caída: solo se registró la corrida ({estado.get('detalle_error')})")
        return

    data = leer_json(DATA_PATH)
    if data is None:
        print(f"ERROR: no existe {DATA_PATH}. Corre scraper_ema_epi.py primero.", file=sys.stderr)
        sys.exit(1)

    documentos = data.get("documentos", [])
    conteos = {"nuevos": 0, "modificados": 0, "sin_cambios": 0}

    for doc in documentos:
        dedupe_key = f"{doc['bundle_id']}_{doc.get('idioma', 'und')}"
        actual = get_documento_actual(dedupe_key)

        if actual is None:
            registrar_cambio(doc, None)
            upsert_documento(doc, dedupe_key)
            conteos["nuevos"] += 1
        elif actual["contenido_hash"] != doc["contenido_hash"]:
            registrar_cambio(doc, actual)
            upsert_documento(doc, dedupe_key)
            conteos["modificados"] += 1
        else:
            # Refresca columnas auxiliares (identificadores, html) sin generar cambio.
            upsert_documento(doc, dedupe_key, contenido_cambio=False)
            conteos["sin_cambios"] += 1

    registrar_corrida(estado, conteos)
    print(
        f"Sync completo: {conteos['nuevos']} nuevos, {conteos['modificados']} modificados, "
        f"{conteos['sin_cambios']} sin cambios (de {len(documentos)} documentos)"
    )


if __name__ == "__main__":
    main()
