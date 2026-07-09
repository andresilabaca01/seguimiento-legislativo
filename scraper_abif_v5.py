#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scraper ABIF v5: agrega soporte robusto para Resultados de Cámara y sesiones realizadas.

Qué hace:
1) Lee el proyectos.json existente.
2) Revisa Cámara vía Datos Abiertos: mociones y mensajes del año actual/anterior.
3) Revisa Cámara vía HTML público: citaciones y RESULTADOS semanales.
4) Fuerza revisión de semanas recientes por URL calculada, aunque el índice no haya actualizado.
5) Detecta proyectos nuevos con posible relevancia bancaria y los deja en data["candidatos"].
6) Actualiza movimientos de proyectos ya seguidos si aparecen en citaciones/resultados.
7) Conserva manualmente los resúmenes, relevancias y datos existentes del monitor.

Uso local/GitHub Actions:
  python scraper_abif_v5.py --input proyectos.json --output proyectos.json
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

TZ = dt.timezone(dt.timedelta(hours=-4))  # referencia Chile invierno; no afecta comparación de fechas ISO
HOY = dt.datetime.now(TZ).date()

CAMARA_BASE = "https://www.camara.cl/"
CAMARA_CITACIONES_TODAS = "https://www.camara.cl/legislacion/comisiones/citaciones_todas.aspx"
CAMARA_RESULTADOS_TODOS = "https://www.camara.cl/legislacion/comisiones/resultados_todos.aspx"
CAMARA_COMISIONES = "https://www.camara.cl/legislacion/comisiones/comisiones_otras.aspx"
CAMARA_OPEN_LEG = "https://opendata.camara.cl/camaradiputados/WServices/WSLegislativo.asmx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ABIF-Monitor-Legislativo/5.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PALABRAS_CLAVE_ABIF = [
    # Bancario/financiero directo
    "banco", "bancos", "bancaria", "bancario", "institucion financiera", "instituciones financieras",
    "financiero", "financiera", "mercado financiero", "cmf", "uaf", "secreto bancario", "reserva bancaria",
    "lavado de activos", "financiamiento del terrorismo", "beneficiario final", "comision para el mercado financiero",
    # crédito / cobranza / garantías
    "credito", "creditos", "hipotecario", "hipotecarios", "consumo", "tasa de interes", "interes", "tmc",
    "tasa maxima", "cobranza", "cobro", "morosidad", "deuda", "deudas", "repactacion", "refinanciamiento",
    "embargo", "inembargable", "inembargabilidad", "garantia", "garantias", "aval", "provisiones", "capital",
    # pagos / productos / contratos
    "tarjeta", "tarjetas", "cuenta corriente", "cheque", "pagare", "letra de cambio", "factura", "factoring",
    "medios de pago", "pago electronico", "pos", "cajero", "cajeros automaticos", "firma electronica",
    "documento electronico", "contrato de adhesion", "sernac", "consumidor", "consumidores", "publicidad", "llamadas",
    # datos / IA / digital
    "datos personales", "proteccion de datos", "tratamiento de datos", "biometria", "inteligencia artificial",
    "algoritmo", "solvencia", "calificacion crediticia", "scoring", "ciberseguridad", "fraude", "fraudes",
    # UF / reajustabilidad
    "unidad de fomento", "uf", "reajustabilidad", "dividendo", "dividendos",
]

CATEGORIAS_ABIF = {
    "Secreto y reserva bancaria": ["secreto bancario", "reserva bancaria", "articulo 154", "levantamiento del secreto"],
    "Datos personales financieros": ["datos personales", "proteccion de datos", "tratamiento de datos", "biometria"],
    "Crédito hipotecario": ["hipotecario", "hipotecarios", "dividendo", "uf", "unidad de fomento", "vivienda"],
    "Crédito de consumo": ["credito de consumo", "creditos de consumo", "tarjeta", "tasa", "interes", "tmc"],
    "Cobranza y ejecución": ["cobranza", "cobro", "juicio ejecutivo", "embargo", "inembargabilidad", "morosidad"],
    "Pagos y transacciones": ["medios de pago", "pago electronico", "pos", "cheque", "pagare", "letra de cambio", "firma electronica"],
    "Fintech / IA financiera": ["inteligencia artificial", "fintech", "algoritmo", "solvencia", "scoring"],
    "Protección al consumidor financiero": ["sernac", "consumidor", "consumidores", "contrato de adhesion", "publicidad"],
    "Lavado de activos / AML": ["uaf", "lavado de activos", "financiamiento del terrorismo", "beneficiario final"],
    "Laboral bancario": ["codigo del trabajo", "negociacion colectiva", "sala cuna", "jornada laboral"],
}


def sin_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def norm_text(s: str) -> str:
    s = sin_tildes(s or "").lower()
    return re.sub(r"\s+", " ", s).strip()


def norm_boletin(b: str) -> str:
    b = (b or "").strip()
    b = b.replace("N°", "").replace("Nº", "").replace("Boletín", "").replace("boletín", "")
    b = b.replace(".", "")
    b = re.sub(r"\s+", " ", b).strip()
    return b


def boletines_en_texto(texto: str) -> List[str]:
    # Acepta 18.340-03, 18340-03, boletín N° 18340-03, etc.
    out = []
    for m in re.finditer(r"(?i)bolet[ií]n(?:es)?\s*(?:n[°º]\s*)?([0-9]{1,2}\.?[0-9]{3}-[0-9]{2})", texto):
        out.append(norm_boletin(m.group(1)))
    for m in re.finditer(r"\b([0-9]{1,2}\.?[0-9]{3}-[0-9]{2})\b", texto):
        out.append(norm_boletin(m.group(1)))
    # dedup preservando orden
    seen, res = set(), []
    for b in out:
        if b not in seen:
            seen.add(b); res.append(b)
    return res


def parse_fecha_any(s: str) -> Optional[str]:
    if not s:
        return None
    s0 = s.strip()
    # ISO
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", s0)
    if m:
        y, mo, d = map(int, m.groups())
        try: return dt.date(y, mo, d).isoformat()
        except ValueError: return None
    # dd/mm/yyyy
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](20\d{2})", s0)
    if m:
        d, mo, y = map(int, m.groups())
        try: return dt.date(y, mo, d).isoformat()
        except ValueError: return None
    # español: 13 julio 2026 / 13 de julio de 2026
    meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
    m = re.search(r"(\d{1,2})(?:\s+de)?\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(20\d{2})", norm_text(s0))
    if m:
        d = int(m.group(1)); mo = meses[m.group(2)]; y = int(m.group(3))
        try: return dt.date(y, mo, d).isoformat()
        except ValueError: return None
    return None


def get_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": "inicial", "proyectos": [], "candidatos": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def request_text(url: str, params: Optional[dict] = None, timeout: int = 25) -> str:
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            if not r.encoding:
                r.encoding = "utf-8"
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.2)
    raise RuntimeError(f"No se pudo leer {url}: {last}")


def xml_records(xml_text: str) -> List[Dict[str, str]]:
    """Convierte respuesta XML ASMX en lista de dicts flexibles."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except Exception:
        root = ET.fromstring(xml_text)

    def tag(e):
        return e.tag.split("}", 1)[-1] if "}" in e.tag else e.tag

    recs: List[Dict[str, str]] = []
    for elem in root.iter():
        children = list(elem)
        if len(children) < 2:
            continue
        d: Dict[str, str] = {}
        for ch in children:
            t = "".join(ch.itertext()).strip()
            if t:
                d[tag(ch)] = re.sub(r"\s+", " ", t)
        joined = " ".join(d.values())
        if boletines_en_texto(joined) and any("titulo" in norm_text(k) or "nombre" in norm_text(k) or "materia" in norm_text(k) for k in d.keys()):
            recs.append(d)
    # dedup por boletín + título
    seen, out = set(), []
    for r in recs:
        joined = " ".join(r.values())
        bs = boletines_en_texto(joined)
        key = (bs[0] if bs else "", r.get("Titulo") or r.get("Nombre") or joined[:60])
        if key not in seen:
            seen.add(key); out.append(r)
    return out


def call_camara_open(method: str, **params) -> List[Dict[str, str]]:
    url = f"{CAMARA_OPEN_LEG}/{method}"
    xml = request_text(url, params=params)
    return xml_records(xml)


def find_field(record: Dict[str, str], *needles: str) -> str:
    needles_n = [norm_text(n) for n in needles]
    for k, v in record.items():
        kn = norm_text(k)
        if any(n in kn for n in needles_n):
            return v
    return ""


def score_abif(texto: str) -> Tuple[int, List[str], List[str]]:
    t = norm_text(texto)
    matches = []
    for kw in PALABRAS_CLAVE_ABIF:
        if norm_text(kw) in t:
            matches.append(kw)
    cats = []
    for cat, kws in CATEGORIAS_ABIF.items():
        if any(norm_text(k) in t for k in kws):
            cats.append(cat)
    return len(matches), matches[:8], cats[:5]


def proyecto_por_boletin(proyectos: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out = {}
    for p in proyectos:
        for b in re.split(r"/| y |,", p.get("boletin", "")):
            nb = norm_boletin(b)
            if nb:
                out[nb] = p
    return out


def add_hist(p: Dict[str, Any], fecha: str, texto: str) -> bool:
    p.setdefault("hist", [])
    key = (fecha, norm_text(texto)[:160])
    for h in p["hist"]:
        if (h.get("f"), norm_text(h.get("t", ""))[:160]) == key:
            return False
    p["hist"].append({"f": fecha, "t": texto})
    if fecha and (not p.get("fecha") or fecha > p.get("fecha")):
        p["fecha"] = fecha
    return True


def merge_candidate(candidatos: List[Dict[str, Any]], cand: Dict[str, Any], existentes: set) -> bool:
    b = norm_boletin(cand.get("boletin", ""))
    if not b or b in existentes:
        return False
    for c in candidatos:
        if norm_boletin(c.get("boletin", "")) == b:
            # enriquecer si llega mejor info
            for k, v in cand.items():
                if v and not c.get(k):
                    c[k] = v
            return False
    candidatos.append(cand)
    existentes.add(b)
    return True


def candidato_from_record(r: Dict[str, str], fuente: str) -> Optional[Dict[str, Any]]:
    joined = " ".join(r.values())
    bs = boletines_en_texto(joined)
    if not bs:
        return None
    titulo = find_field(r, "titulo", "nombre", "materia") or joined[:220]
    fecha = parse_fecha_any(find_field(r, "fecha"))
    iniciativa = find_field(r, "iniciativa", "tipo")
    score, kws, cats = score_abif(joined + " " + titulo)
    if score == 0:
        return None
    return {
        "boletin": bs[0],
        "titulo": titulo,
        "desc": f"Detectado automáticamente desde Cámara ({fuente}). Revisar texto oficial y completar análisis ABIF.",
        "camara": "Cámara de Diputados",
        "etapa": "Ingreso / por revisar",
        "urgencia": "Sin urgencia",
        "impacto": " · ".join(cats[:3]) if cats else "Por evaluar",
        "fecha": fecha,
        "match": kws,
        "categorias_abif": cats,
        "fuente": fuente,
        "iniciativa": iniciativa,
    }


def scan_open_data_camara(data: Dict[str, Any], years_back: int = 1) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    candidatos = data.setdefault("candidatos", [])
    existentes = set(proyecto_por_boletin(proyectos).keys()) | {norm_boletin(c.get("boletin", "")) for c in candidatos}
    logs = []
    nuevos = 0
    for year in range(HOY.year - years_back, HOY.year + 1):
        for method, fuente in [("retornarMocionesXAnno", "Cámara Datos Abiertos · Mociones"), ("retornarMensajesXAnno", "Cámara Datos Abiertos · Mensajes")]:
            try:
                recs = call_camara_open(method, prmAnno=str(year))
                logs.append(f"{fuente} {year}: {len(recs)} registros leídos")
                for r in recs:
                    cand = candidato_from_record(r, fuente)
                    if cand and merge_candidate(candidatos, cand, existentes):
                        nuevos += 1
            except Exception as e:
                logs.append(f"ERROR {fuente} {year}: {e}")
    return nuevos, logs


def recent_week_links(index_url: str, max_links: int = 8) -> List[str]:
    html = request_text(index_url)
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        txt = norm_text(a.get_text(" "))
        href = a.get("href", "")
        # Cámara usa links tipo citaciones_semana.aspx?prmSemana=2026-28 y resultados_semana.aspx?prmSemana=2026-28
        if "semana del" in txt or "_semana.aspx" in href:
            links.append(urljoin(index_url, href))
    # dedup preservando orden
    out = []
    seen = set()
    for u in links:
        if u not in seen:
            seen.add(u); out.append(u)
    return out[:max_links]


def semana_urls_calculadas(tipo: str, weeks_back: int = 8, weeks_forward: int = 2) -> List[str]:
    """Genera URLs semanales aunque el índice de Cámara todavía no haya publicado el link.
    tipo: 'citaciones' o 'resultados'.
    """
    if tipo not in {"citaciones", "resultados"}:
        return []
    pagina = "citaciones_semana.aspx" if tipo == "citaciones" else "resultados_semana.aspx"
    urls = []
    # se calcula usando la semana ISO; Cámara usa prmSemana=YYYY-WW, sin cero a la izquierda
    lunes_actual = HOY - dt.timedelta(days=HOY.weekday())
    for delta in range(-weeks_back, weeks_forward + 1):
        d = lunes_actual + dt.timedelta(weeks=delta)
        iso = d.isocalendar()
        urls.append(f"https://www.camara.cl/legislacion/comisiones/{pagina}?prmSemana={iso.year}-{iso.week}")
    return urls


def day_headings(text: str) -> List[Tuple[int, str]]:
    meses = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
    patron = re.compile(r"(?i)(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo),?\s+(\d{1,2})\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+de\s+(20\d{2})")
    out = []
    for m in patron.finditer(text):
        dd = int(m.group(2)); mm = meses[norm_text(m.group(3))]; yy = int(m.group(4))
        try:
            out.append((m.start(), dt.date(yy, mm, dd).isoformat()))
        except ValueError:
            pass
    return out


def fecha_para_posicion(text: str, pos: int, fallback: str) -> str:
    heads = day_headings(text)
    fecha = fallback
    for hp, hf in heads:
        if hp <= pos:
            fecha = hf
        else:
            break
    return fecha


def resumir_contexto_camara(ctx: str, max_len: int = 1100) -> str:
    ctx = re.sub(r"\s+", " ", ctx or " ").strip()
    # corta basura de navegación si aparece
    for marker in ["ACTIVIDAD LEGISLATIVA", "TRANSPARENCIA", "CÁMARA DE DIPUTADAS"]:
        i = ctx.find(marker)
        if i > 250:
            ctx = ctx[:i].strip()
    if len(ctx) > max_len:
        return ctx[:max_len-3] + "..."
    return ctx


def extract_fecha_from_page(text: str) -> str:
    # prioriza primera fecha explícita; si falla, hoy
    f = parse_fecha_any(text[:1000])
    return f or HOY.isoformat()


def invitado_contexto(ctx: str) -> str:
    # Extrae una frase de invitados si existe.
    m = re.search(r"(?i)(se encuentran? invitad[oa]s?.{0,350}|para .*? se encuentra invitad[oa].{0,350}|invitad[oa]s?\s*[:：].{0,350})", ctx)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def scan_html_camara(data: Dict[str, Any]) -> Tuple[int, List[str]]:
    proyectos = data.setdefault("proyectos", [])
    candidatos = data.setdefault("candidatos", [])
    pmap = proyecto_por_boletin(proyectos)
    existentes = set(pmap.keys()) | {norm_boletin(c.get("boletin", "")) for c in candidatos}
    logs = []
    cambios = 0

    fuentes = [
        ("citaciones", CAMARA_CITACIONES_TODAS),
        ("resultados", CAMARA_RESULTADOS_TODOS),
    ]

    urls_tipo: List[Tuple[str, str]] = []
    for tipo, idx in fuentes:
        try:
            # Links que entrega Cámara desde el índice público.
            for u in recent_week_links(idx, max_links=14):
                urls_tipo.append((tipo, u))
            logs.append(f"Índice Cámara {tipo}: links semanales obtenidos")
        except Exception as e:
            logs.append(f"ERROR leyendo índice Cámara {tipo} {idx}: {e}")
        # Blindaje: revisar semanas recientes aunque el índice falle o se demore en actualizar.
        for u in semana_urls_calculadas(tipo, weeks_back=8, weeks_forward=2):
            urls_tipo.append((tipo, u))

    # Dedup preservando orden por URL + tipo
    dedup: List[Tuple[str, str]] = []
    seen_pairs = set()
    for tipo, u in urls_tipo:
        key = (tipo, u)
        if key not in seen_pairs:
            seen_pairs.add(key)
            dedup.append((tipo, u))
    urls_tipo = dedup[:50]

    for tipo, url in urls_tipo:
        try:
            html = request_text(url)
            soup = BeautifulSoup(html, "html.parser")
            # Separador de espacio para mantener posiciones; no usar saltos que distorsionen regex.
            text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
            fecha_page = extract_fecha_from_page(text)
            boletines = list(re.finditer(r"(?i)bolet[ií]n(?:es)?\s*(?:n[°º]\s*)?([0-9]{1,2}\.?[0-9]{3}-[0-9]{2})", text))
            logs.append(f"Cámara {tipo} leído: {url} · boletines encontrados: {len(boletines)}")

            for m in boletines:
                b = norm_boletin(m.group(1))
                fecha = fecha_para_posicion(text, m.start(), fecha_page)
                # Ventana amplia: en resultados, el acuerdo/votación suele quedar después del boletín.
                ctx = text[max(0, m.start()-520):m.end()+950]
                ctx = resumir_contexto_camara(ctx)
                invitados = invitado_contexto(ctx)
                etiqueta = "Resultado de comisión" if tipo == "resultados" else "Citación de comisión"
                base = f"[Cámara · {etiqueta} · {fecha}] {ctx}"
                if invitados and invitados not in base:
                    base += f" Invitados: {invitados}"
                if url not in base:
                    base += f" Fuente: {url}"

                if b in pmap:
                    if add_hist(pmap[b], fecha, base):
                        cambios += 1
                else:
                    score, kws, cats = score_abif(ctx)
                    if score > 0:
                        cand = {
                            "boletin": b,
                            "titulo": f"Proyecto detectado en {etiqueta.lower()} de Cámara — revisar ficha oficial",
                            "desc": base,
                            "camara": "Cámara de Diputados",
                            "etapa": f"{etiqueta} · por revisar",
                            "urgencia": "Sin urgencia",
                            "impacto": " · ".join(cats[:3]) if cats else "Por evaluar",
                            "fecha": fecha,
                            "match": kws,
                            "categorias_abif": cats,
                            "fuente": url,
                            "tipo_evento": tipo,
                        }
                        if merge_candidate(candidatos, cand, existentes):
                            cambios += 1
        except Exception as e:
            logs.append(f"ERROR Cámara {tipo} {url}: {e}")
    return cambios, logs

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="proyectos.json")
    ap.add_argument("--output", default="proyectos.json")
    ap.add_argument("--years-back", type=int, default=1)
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    data = get_json(inp)
    data.setdefault("proyectos", [])
    data.setdefault("candidatos", [])

    logs_all = []
    nuevos_open, logs = scan_open_data_camara(data, years_back=args.years_back)
    logs_all.extend(logs)
    cambios_html, logs = scan_html_camara(data)
    logs_all.extend(logs)

    data["generado"] = dt.datetime.now(TZ).replace(microsecond=0).isoformat()
    data["version"] = dt.datetime.now(TZ).strftime("%Y-%m-%d-%H%M-abif-v5")
    data["total"] = len(data.get("proyectos", []))
    data["cambios_detectados"] = int(data.get("cambios_detectados") or 0) + nuevos_open + cambios_html
    data["fuentes_revision"] = {
        "camara_open_data_nuevos_candidatos": nuevos_open,
        "camara_html_cambios_o_candidatos": cambios_html,
        "logs": logs_all[-80:],
    }

    save_json(out, data)
    print(json.dumps({
        "ok": True,
        "output": str(out),
        "proyectos": len(data.get("proyectos", [])),
        "candidatos": len(data.get("candidatos", [])),
        "nuevos_candidatos_open_data": nuevos_open,
        "cambios_html": cambios_html,
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
