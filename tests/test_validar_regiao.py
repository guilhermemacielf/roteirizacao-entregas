# -*- coding: utf-8 -*-
"""Testa a validacao geografica que impede rua homonima de OUTRA cidade
(ex.: Rua Goias em Divinopolis) de envenenar o geocode de um endereco de BH.
Sem rede: monkeypatcha os provedores."""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from motor import geocode as G

DIVINOPOLIS = (-20.1386, -44.8841)   # ~100km de BH (rua homonima)
BH_CENTRO = (-19.9259, -43.9355)     # Rua Goias, Centro, BH (certo)
ENDERECO = "R. Goias 317 Apto 2106 Centro Belo Horizonte MG 30190-030"


def test_coord_plausivel():
    assert G._coord_plausivel(BH_CENTRO) is True
    assert G._coord_plausivel(DIVINOPOLIS) is False
    # Cidades reais da RMBH tem que PASSAR (nao sao rejeitadas por engano):
    assert G._coord_plausivel((-19.9320, -44.0536)) is True   # Contagem
    assert G._coord_plausivel((-19.9858, -43.8464)) is True   # Nova Lima
    assert G._coord_plausivel((-20.1446, -44.1995)) is True   # Brumadinho
    assert G._coord_plausivel((-19.5170, -43.7450)) is True   # Jaboticatubas
    assert G._coord_plausivel(None) is False
    print("OK _coord_plausivel: BH/RMBH passam, Divinopolis rejeitada")


def test_cascata_descarta_divinopolis():
    """v1 (Nominatim) e Google devolvem Divinopolis (homonima); o CEP
    (BrasilAPI) devolve o ponto certo de BH. A cascata TEM que descartar
    Divinopolis e cair no ponto de BH — nao aceitar a homonima."""
    with mock.patch.object(G, "_consultar_nominatim", return_value=DIVINOPOLIS), \
         mock.patch.object(G, "_consultar_google_maps", return_value=DIVINOPOLIS), \
         mock.patch.object(G, "_consultar_brasilapi_cep", return_value=BH_CENTRO), \
         mock.patch.object(G, "GOOGLE_MAPS_API_KEY", "fake-key"), \
         mock.patch.object(G.time, "sleep", lambda *_: None):
        coord = G._consultar_em_cascata(ENDERECO)
    assert coord == BH_CENTRO, f"esperava BH, veio {coord}"
    assert not G._coord_plausivel(DIVINOPOLIS)
    print("OK cascata: Divinopolis descartada, caiu no ponto de BH via CEP")


def test_sem_fix_pegaria_divinopolis():
    """Sanidade: SEM a validacao, a v1 com Divinopolis seria aceita.
    Confirma que o teste acima so passa por causa do guard."""
    with mock.patch.object(G, "_consultar_nominatim", return_value=DIVINOPOLIS), \
         mock.patch.object(G, "_coord_plausivel", return_value=True), \
         mock.patch.object(G, "GOOGLE_MAPS_API_KEY", ""), \
         mock.patch.object(G.time, "sleep", lambda *_: None):
        coord = G._consultar_em_cascata(ENDERECO)
    assert coord == DIVINOPOLIS, "sem o guard, a v1 homonima seria aceita"
    print("OK sanidade: sem o guard a homonima passaria (prova que o guard atua)")


if __name__ == "__main__":
    test_coord_plausivel()
    test_cascata_descarta_divinopolis()
    test_sem_fix_pegaria_divinopolis()
    print("\nTODOS OK")
