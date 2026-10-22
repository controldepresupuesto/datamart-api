# -*- coding: utf-8 -*-
"""Pruebas de los filtros de la API (22-oct-2026). Sin base: fijan el SQL que arma build_filters. Que el resultado sea el
MISMO que el de antes se midio en produccion (12 filtros de 12 endpoints, mismas filas) y queda escrito en main.py.

    pip install -r requirements.txt pytest && python -m pytest -q
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://nadie@localhost/nada")   # main.py no se conecta al importarse

from starlette.datastructures import QueryParams  # noqa: E402

import main  # noqa: E402


def test_un_valor_usa_el_indice_y_la_comparacion_de_siempre():
    w, p = main.build_filters("contratos.actas", QueryParams("NoContrato=77&limit=10"))
    assert w == ["endpoint = %s", "(data @> %s::jsonb OR data @> %s::jsonb)", "data->>%s = ANY(%s)"]
    assert p == ["contratos.actas", '{"NoContrato": "77"}', '{"NoContrato": 77}', "NoContrato", ["77"]]


def test_varios_valores_del_mismo_campo():
    """Antes `dict(query_params)` se quedaba con el ULTIMO: ?NoContrato=a&NoContrato=b devolvia solo los de b."""
    w, p = main.build_filters("contratos.actas", QueryParams("NoContrato=77&NoContrato=45&NoContrato=77"))
    assert p[-1] == ["77", "45"] and len(p) == 1 + 4 + 2           # sin repetir el 77
    assert w[1].count("data @>") == 4


def test_texto_numero_booleano_y_lo_que_no_tiene_forma_segura():
    _, p = main.build_filters("x", QueryParams("Estado=SI&Vigente=true&Codigo=007&Lista=[1, 2]"))
    assert '{"Estado": "SI"}' in p and '{"Vigente": true}' in p
    assert '{"Codigo": 007}' not in p                     # 007 no es un numero JSON valido: solo como cadena
    assert "Lista" in p and '{"Lista": "[1, 2]"}' not in p  # arreglo: filtra como antes, sin contencion


def test_reservados_y_dict_siguen_igual():
    w, p = main.build_filters("pyg", {"empresa": "cimelec", "limit": "5", "Cuenta": "4105"})
    assert w[:2] == ["endpoint = %s", "empresa = %s"] and p[:2] == ["pyg", "cimelec"]
    assert "limit" not in p and p[-1] == ["4105"]
