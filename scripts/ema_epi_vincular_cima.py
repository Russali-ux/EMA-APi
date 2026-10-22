"""
Propone vínculos ePI (bundle + idioma) -> CIMA (nregistro) y los guarda en
ema_epi_cima_vinculo con estado 'propuesto'. Nunca pisa un vínculo ya
verificado o rechazado por un usuario (insert con ignore-duplicates).

Métodos, de más a menos confiable:
  1. numero_eu (alta): EU/1/16/1102/001 -> nregistro 1161102001.
     Regla verificada contra CIMA (22/10/2026): '1' + yy + nnnn + ppp
     (nnnn con ceros a la izquierda). Si el ePI solo trae el número de
     producto (sin /ppp) se prueban las presentaciones 001, 002, ...
     Cada candidato se confirma con GET /cima/rest/medicamento.
  2. identificador (alta): cualquier `identifier.value` numérico del
     Bundle que exista como nregistro en CIMA (caso AEMPS nacional).
  3. nombre (baja): solo si 1 y 2 no dieron nada; busca en CIMA por la
     primera palabra del nombre y exige que comparta principio activo /
     nombre. Requiere revisión manual.

Lee data/ema_epi_data.json (salida del scraper); no depende de Alertas-analyzer.

Uso:
    python scripts/ema_epi_vincular_cima.py            # escribe en Supabase
    python scripts/ema_epi_vincular_cima.py --dry-run  # solo imprime
"""

import argparse
import json
import os
import re
import sys
import time

import requests

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DATA_PATH = os.path.join(DATA_DIR, "ema_epi_data.json")
ESTADO_PATH = os.path.join(DATA_DIR, "ema_epi_estado.json")

CIMA_BASE = "https://cima.aemps.es/cima/rest"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

RE_NUMERO_EU = re.compile(r"^EU/([12])/(\d{2})/(\d{3,4})(?:/(\d{3}))?$")
MAX_PRESENTACIONES = 60          # tope de presentaciones a probar por producto
MAX_HUECOS_SEGUIDOS = 10         # se corta tras N presentaciones inexistentes seguidas
PAUSA_CIMA = 0.15                # cortesía con la API de CIMA


class Cima:
    def __init__(self):
        self.session = requests.Session()
        self.cache: dict[str, dict | None] = {}

    def medicamento(self, nregistro: str) -> dict | None:
        if nregistro in self.cache:
            return self.cache[nregistro]
        datos = None
        for intento in range(3):
            try:
                resp = self.session.get(
                    f"{CIMA_BASE}/medicamento", params={"nregistro": nregistro}, timeout=30
                )
                if resp.status_code == 204 or not resp.content:
                    break
                resp.raise_for_status()
                datos = resp.json()
                break
            except requests.RequestException:
                time.sleep(2 * (intento + 1))
        time.sleep(PAUSA_CIMA)
        self.cache[nregistro] = datos
        return datos

    def buscar_por_nombre(self, nombre: str) -> list[dict]:
        try:
            resp = self.session.get(
                f"{CIMA_BASE}/medicamentos",
                params={"nombre": nombre, "tamanioPagina": 50},
                timeout=30,
            )
            resp.raise_for_status()
            return (resp.json() or {}).get("resultados", []) if resp.content else []
        except requests.RequestException:
            return []


def nregistro_desde_eu(numero_eu: str) -> tuple[str, bool] | None:
    """EU/1/16/1102/001 -> ('1161102001', True); EU/1/16/1102 -> ('1161102', False)."""
    m = RE_NUMERO_EU.match(numero_eu)
    if not m:
        return None
    tipo, anio, producto, presentacion = m.groups()
    base = f"{tipo}{anio}{int(producto):04d}"
    return (base + presentacion, True) if presentacion else (base, False)


def candidatos_numero_eu(cima: Cima, numeros_eu: list[str]) -> list[tuple[str, dict, str]]:
    encontrados = []
    vistos = set()
    for numero in numeros_eu:
        conv = nregistro_desde_eu(numero)
        if not conv:
            continue
        base, completo = conv
        if completo:
            pruebas = [base]
        else:
            pruebas = [f"{base}{p:03d}" for p in range(1, MAX_PRESENTACIONES + 1)]
        huecos = 0
        for nreg in pruebas:
            if nreg in vistos:
                continue
            med = cima.medicamento(nreg)
            if med:
                vistos.add(nreg)
                encontrados.append((nreg, med, numero))
                huecos = 0
            else:
                huecos += 1
                if not completo and huecos >= MAX_HUECOS_SEGUIDOS:
                    break
    return encontrados


def candidatos_identificador(cima: Cima, identificadores: list[dict]) -> list[tuple[str, dict, str]]:
    encontrados = []
    for ident in identificadores or []:
        valor = str(ident.get("value") or "").strip()
        if not re.fullmatch(r"\d{5,10}", valor):
            continue
        med = cima.medicamento(valor)
        if med:
            encontrados.append((valor, med, f"{ident.get('system') or 'identifier'}={valor}"))
    return encontrados


def _normalizar(texto: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (texto or "").lower())


def candidatos_nombre(cima: Cima, nombre: str) -> list[tuple[str, dict, str]]:
    palabras = [p for p in _normalizar(nombre).split() if len(p) >= 4]
    if not palabras:
        return []
    clave = palabras[0]
    resultados = cima.buscar_por_nombre(clave)
    encontrados = []
    for med in resultados:
        nombre_cima = _normalizar(med.get("nombre", ""))
        pactivos = _normalizar(med.get("pactivos", ""))
        if clave in nombre_cima or clave in pactivos:
            encontrados.append((med["nregistro"], med, f"nombre~'{clave}'"))
    return encontrados[:10]


def proponer_vinculos(cima: Cima, doc: dict) -> list[dict]:
    base = {"bundle_id": doc["bundle_id"], "idioma": doc.get("idioma") or "und"}

    for metodo, candidatos in (
        ("numero_eu", lambda: candidatos_numero_eu(cima, doc.get("numeros_eu") or [])),
        ("identificador", lambda: candidatos_identificador(cima, doc.get("identificadores") or [])),
    ):
        hallados = candidatos()
        if hallados:
            return [
                {**base, "nregistro": nreg, "nombre_cima": med.get("nombre"),
                 "laboratorio_cima": med.get("labtitular"), "metodo_match": metodo,
                 "confianza": "alta", "evidencia": evidencia}
                for nreg, med, evidencia in hallados
            ]

    return [
        {**base, "nregistro": nreg, "nombre_cima": med.get("nombre"),
         "laboratorio_cima": med.get("labtitular"), "metodo_match": "nombre",
         "confianza": "baja", "evidencia": evidencia}
        for nreg, med, evidencia in candidatos_nombre(cima, doc.get("nombre_medicamento") or "")
    ]


def guardar(vinculos: list[dict]):
    if not vinculos:
        return
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/ema_epi_cima_vinculo",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            # No pisa vínculos existentes (p. ej. ya verificados/rechazados)
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        },
        params={"on_conflict": "bundle_id,idioma,nregistro"},
        json=vinculos,
        timeout=60,
    )
    if resp.status_code >= 400:
        print(f"Supabase -> {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if os.path.exists(ESTADO_PATH):
        with open(ESTADO_PATH, encoding="utf-8") as f:
            if json.load(f).get("estado") == "fuente_caida":
                print("Fuente EMA caída en esta corrida: no hay datos nuevos que vincular.")
                return

    if not os.path.exists(DATA_PATH):
        print(f"No existe {DATA_PATH}; nada que vincular.")
        return

    if not args.dry_run and (not SUPABASE_URL or not SUPABASE_KEY):
        print("ERROR: configura SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY", file=sys.stderr)
        sys.exit(1)

    with open(DATA_PATH, encoding="utf-8") as f:
        documentos = json.load(f).get("documentos", [])

    cima = Cima()
    total = {"alta": 0, "baja": 0, "sin_match": 0}
    vistos = set()
    for doc in documentos:
        clave = (doc["bundle_id"], doc.get("idioma"))
        if clave in vistos:
            continue
        vistos.add(clave)
        vinculos = proponer_vinculos(cima, doc)
        if not vinculos:
            total["sin_match"] += 1
            continue
        total[vinculos[0]["confianza"]] = total.get(vinculos[0]["confianza"], 0) + 1
        for v in vinculos:
            print(f"{v['confianza']:5} {v['metodo_match']:13} {doc.get('nombre_medicamento','')[:40]:40} -> {v['nregistro']} ({v['evidencia']})")
        if not args.dry_run:
            guardar(vinculos)

    print(
        f"Vinculación CIMA: {total['alta']} documentos con match alto, "
        f"{total['baja']} solo por nombre (revisar), {total['sin_match']} sin match"
    )


if __name__ == "__main__":
    main()
