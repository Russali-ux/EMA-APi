"""Tests sin red: resiliencia del cliente, hash, vinculación EU -> CIMA y resumen de cambios."""

import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import ema_epi_client  # noqa: E402
import ema_epi_sync_supabase as sync  # noqa: E402
import ema_epi_vincular_cima as vinc  # noqa: E402
import scraper_ema_epi as scraper  # noqa: E402


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch):
    monkeypatch.setattr(ema_epi_client.time, "sleep", lambda *_: None)


def cliente_con(respuestas):
    c = ema_epi_client.EmaEpiClient(base_url="https://x")
    llamadas = []

    def fake_get(url, params=None, timeout=None):
        llamadas.append(url)
        return respuestas.pop(0) if len(respuestas) > 1 else respuestas[0]

    c.session.get = fake_get
    return c, llamadas


def test_500_persistente_es_fuente_caida():
    error_ema = {"message": "The format of value ' ' is invalid.", "errorStack": "..."}
    c, llamadas = cliente_con([FakeResp(500, error_ema)])
    with pytest.raises(ema_epi_client.EmaEpiFuenteCaida) as exc:
        c.get_all_lists()
    assert exc.value.http_status == 500
    assert "format of value" in exc.value.detalle
    assert len(llamadas) == ema_epi_client.MAX_RETRIES


def test_500_transitorio_se_recupera():
    c, _ = cliente_con([FakeResp(500, {}), FakeResp(200, {"entry": [{"resource": {"resourceType": "List", "id": "L1"}}]})])
    assert [x["id"] for x in c.get_all_lists()] == ["L1"]


def test_404_no_se_reintenta_y_falla():
    c, llamadas = cliente_con([FakeResp(404, {"message": "no"})])
    with pytest.raises(requests.HTTPError):
        c.list_by_id("x")
    assert len(llamadas) == 1


def test_base_url_por_defecto(monkeypatch):
    monkeypatch.delenv("EMA_EPI_BASE_URL", raising=False)
    assert ema_epi_client.EmaEpiClient().base_url == ema_epi_client.DEFAULT_BASE_URL


def test_scraper_fuente_caida_no_falla(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(scraper, "OUTPUT_PATH", str(tmp_path / "ema_epi_data.json"))
    monkeypatch.setattr(scraper, "ESTADO_PATH", str(tmp_path / "ema_epi_estado.json"))
    salida = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(salida))
    monkeypatch.setattr(sys, "argv", ["scraper"])

    class Caido:
        def get_all_lists(self):
            raise ema_epi_client.EmaEpiFuenteCaida("/List", 500, "The format of value ' ' is invalid.")

    monkeypatch.setattr(scraper, "EmaEpiClient", Caido)
    scraper.main()  # no debe lanzar ni hacer sys.exit

    estado = json.loads((tmp_path / "ema_epi_estado.json").read_text())
    assert estado["estado"] == "fuente_caida"
    assert estado["http_status"] == 500
    assert not (tmp_path / "ema_epi_data.json").exists()  # snapshot anterior intacto
    assert "fuente_ok=false" in salida.read_text()


def test_hash_ignora_html_y_codigo():
    a = [{"nivel": 0, "titulo": "4.3", "texto": "x", "codigo": "4.3", "html": "<div>x</div>"}]
    b = [{"nivel": 0, "titulo": "4.3", "texto": "x", "codigo": None, "html": "<div><b>x</b></div>"}]
    assert scraper.calcular_hash(a) == scraper.calcular_hash(b)


def test_extraer_codigo_e_identificadores():
    assert scraper.extraer_codigo_seccion({"title": "4.3 Contraindicaciones"}) == "4.3"
    assert scraper.extraer_codigo_seccion({"code": {"coding": [{"code": "P2"}]}, "title": "x"}) == "P2"
    bundle = {"entry": [{"resource": {"identifier": [{"system": "s", "value": "EU/1/16/1102/001"}]}}]}
    assert scraper.recolectar_identificadores(bundle) == [{"system": "s", "value": "EU/1/16/1102/001"}]
    assert scraper.extraer_numeros_eu(bundle) == ["EU/1/16/1102/001"]


def test_numero_eu_a_nregistro():
    # Caso real verificado en CIMA: Bortezomib SUN EU/1/16/1102/001 -> 1161102001
    assert vinc.nregistro_desde_eu("EU/1/16/1102/001") == ("1161102001", True)
    assert vinc.nregistro_desde_eu("EU/1/07/422") == ("1070422", False)
    assert vinc.nregistro_desde_eu("ES/123") is None


def test_vinculo_por_numero_eu_prueba_presentaciones():
    class FakeCima:
        def medicamento(self, nreg):
            return {"nombre": "X", "labtitular": "L"} if nreg in {"1070422001", "1070422003"} else None

    hallados = vinc.candidatos_numero_eu(FakeCima(), ["EU/1/07/422"])
    assert [h[0] for h in hallados] == ["1070422001", "1070422003"]


def test_resumen_cambio():
    antes = [{"codigo": "4.3", "titulo": "4.3 Contraindicaciones", "texto": "a"},
             {"codigo": "4.8", "titulo": "4.8 Reacciones adversas", "texto": "b"}]
    despues = [{"codigo": "4.3", "titulo": "4.3 Contraindicaciones", "texto": "a"},
               {"codigo": "4.8", "titulo": "4.8 Reacciones adversas", "texto": "b + nueva RAM"},
               {"codigo": "4.9", "titulo": "4.9 Sobredosis", "texto": "c"}]
    r = sync.resumir_cambio(antes, despues)
    assert "Modificadas (1): 4.8 Reacciones adversas" in r
    assert "Agregadas (1): 4.9 Sobredosis" in r
    assert sync.resumir_cambio(None, despues) is None
