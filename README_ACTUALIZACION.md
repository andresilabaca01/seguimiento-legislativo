# Monitor Legislativo ABIF v7.1

## Qué debe quedar en el repositorio

Para la estructura actual del repositorio `andresilabaca01/seguimiento-legislativo`, basta con mantener:

- `.github/workflows/actualizar-monitor.yml`
- `proyectos.json`
- `proyectos_semilla.json`
- `requirements.txt`
- `scraper_abif_v7.py`

Los scripts antiguos `scraper_abif_v4.py`, `scraper_abif_v5.py`, `scraper_abif_v6.py` y `scraper_legislativo.py` pueden eliminarse una vez que el workflow nuevo esté subido, porque `actualizar-monitor.yml` ejecuta exclusivamente `scraper_abif_v7.py`.

## Proyectos incorporados expresamente

Esta versión incluye como seguimiento obligatorio:

- Boletín **18.216-05** — Para la reconstrucción nacional y el desarrollo económico y social.
- Boletín **18.524-05** — Extiende vigencia y aumenta cobertura de la ley N° 21.748 y modifica normas que indica.

El scraper contiene ambos proyectos como respaldo. Si por error se reemplaza `proyectos.json` por una base que no los contenga, el v7.1 los vuelve a incorporar en la siguiente corrida.

El filtro de fecha desde **01/06/2026** se aplica solamente a la **bandeja de candidatos nuevos**. No elimina proyectos que ABIF haya decidido seguir manualmente, por lo que el boletín 18.216-05 se mantiene aunque haya ingresado el 22/04/2026.

## Puesta en marcha

1. Hacer backup o descargar ZIP del repositorio actual.
2. Eliminar los cuatro scrapers antiguos.
3. Reemplazar `proyectos.json`, `proyectos_semilla.json` y `requirements.txt`.
4. Subir `scraper_abif_v7.py`.
5. Reemplazar el workflow por `.github/workflows/actualizar-monitor.yml`.
6. Hacer un solo commit.
7. Ir a **Actions → Actualizar monitor legislativo ABIF → Run workflow**.
8. Confirmar que la ejecución finalice en verde.
9. Abrir la URL pública de `proyectos.json` y comprobar que `generado` corresponda a la corrida recién ejecutada.
10. Abrir el Monitor y pulsar **Sincronizar ahora**.

## Resultado esperado

El JSON debe mostrar 98 proyectos únicos antes de cualquier candidato nuevo incorporado posteriormente por decisión del usuario. Los boletines 18.216-05 y 18.524-05 deben aparecer dentro de `proyectos`, no en `candidatos`.
