"""
Sincroniza data/ema_epi_data.json (generado por scraper_ema_epi.py) contra
Supabase:
  - upsert en ema_epi_documentos (estado actual, dedupe_key = bundle_id+idioma)
  - insert en ema_epi_cambios cuando el hash difiere del guardado

Requiere las variables de entorno:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ema_epi_data.json")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def get_documento_actual(dedupe_key: str) -> dict | None:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/ema_epi_documentos",
        headers=supabase_headers(),
        params={"dedupe_key": f"eq.{dedupe_key}", "select": "*"},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def upsert_documento(doc: dict, dedupe_key: str):
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "org_id": doc["org_id"],
        "agencia": doc["agencia"],
        "pi_list_id": doc.get("pi_list_id", ""),
        "bundle_id": doc["bundle_id"],
        "nombre_medicamento": doc.get("nombre_medicamento"),
        "tipo_documento": doc.get("tipo_documento"),
        "idioma": doc.get("idioma"),
        "contenido_hash": doc["contenido_hash"],
        "contenido_secciones": doc.get("secciones"),
        "dominio": doc.get("dominio"),
        "fecha_ultima_actualizacion": now,
        "dedupe_key": dedupe_key,
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/ema_epi_documentos",
        headers={**supabase_headers(), "Prefer": "resolution=merge-duplicates"},
        params={"on_conflict": "dedupe_key"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def registrar_cambio(doc: dict, hash_anterior: str | None):
    payload = {
        "bundle_id": doc["bundle_id"],
        "nombre_medicamento": doc.get("nombre_medicamento"),
        "tipo_documento": doc.get("tipo_documento"),
        "agencia": doc["agencia"],
        "idioma": doc.get("idioma"),
        "tipo_cambio": "nuevo_documento" if hash_anterior is None else "contenido_modificado",
        "hash_anterior": hash_anterior,
        "hash_nuevo": doc["contenido_hash"],
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/ema_epi_cambios",
        headers=supabase_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(
            "ERROR: configura SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr
        )
        sys.exit(1)

    if not os.path.exists(DATA_PATH):
        print(f"ERROR: no existe {DATA_PATH}. Corre scraper_ema_epi.py primero.")
        sys.exit(1)

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    documentos = data.get("documentos", [])
    n_nuevos = 0
    n_cambiados = 0
    n_sin_cambios = 0

    for doc in documentos:
        dedupe_key = f"{doc['bundle_id']}_{doc.get('idioma', 'und')}"
        actual = get_documento_actual(dedupe_key)

        if actual is None:
            registrar_cambio(doc, hash_anterior=None)
            upsert_documento(doc, dedupe_key)
            n_nuevos += 1
        elif actual["contenido_hash"] != doc["contenido_hash"]:
            registrar_cambio(doc, hash_anterior=actual["contenido_hash"])
            upsert_documento(doc, dedupe_key)
            n_cambiados += 1
        else:
            n_sin_cambios += 1

    print(
        f"Sync completo: {n_nuevos} nuevos, {n_cambiados} modificados, "
        f"{n_sin_cambios} sin cambios (de {len(documentos)} documentos)"
    )


if __name__ == "__main__":
    main()
