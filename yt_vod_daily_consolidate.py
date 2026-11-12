#!/usr/bin/env python3
"""
YouTube VOD Tracker -> Consolidacion diaria + purga de VOD Snapshots

Corre UNA VEZ POR DIA (recomendado: 4:05am hora Argentina = 07:05 UTC, un
rato despues de la corrida horaria de las 4am de yt_vod_tracker.py, para que
ese snapshot ya este guardado). Para cada fuente en "VOD Sources":

  1) Si Source Type = Channel: lee la cantidad actual de suscriptores
     (channels.list) y la guarda en "Subscribers (last)".
  2) Calcula las "vistas acumuladas del dia" = para cada video de esa fuente,
     (ultima vista conocida menos primera vista conocida) dentro de lo que hay
     ahora mismo en VOD Snapshots. Como esta tabla se purga por completo al
     final de cada corrida de este script, en la practica "lo que hay ahora"
     es siempre la ventana desde la ultima consolidacion hasta esta (~24hs).
     LIMITACION: un video recien descubierto en esta misma ventana (1 sola
     medicion) aporta 0 a este total, porque no hay una medicion anterior con
     la cual comparar -- es una aproximacion conservadora, no un conteo exacto
     de "vistas nuevas de hoy" para contenido publicado el mismo dia.
  3) Escribe 1 fila en "VOD Daily" por fuente (fecha, suscriptores si aplica,
     vistas acumuladas, cantidad de videos con medicion ese dia).
  4) Marca "Last Daily Consolidation Date" = hoy (hora Argentina) en la fuente,
     para que no se vuelva a correr 2 veces el mismo dia si el workflow se
     dispara de mas.
  5) Al final, borra TODOS los registros de VOD Snapshots.

Requisitos:
  pip install requests

Variables de entorno requeridas:
  YOUTUBE_API_KEY
  AIRTABLE_TOKEN
"""

import os
import sys
import time
import datetime
from collections import defaultdict

import requests

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

BASE_ID = "appFvqj21Yy73Bjcn"

TBL_VOD_SOURCES = "tblINSjlF3geh6n83"
TBL_VOD_VIDEOS = "tblKIduNLgWQDEpNx"
TBL_VOD_SNAPSHOTS = "tbldk490qYDwR6Iny"
TBL_VOD_DAILY = "tblPFEzlK739fija0"

ARGENTINA_UTC_OFFSET_HOURS = -3

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()

AIRTABLE_API = "https://api.airtable.com/v0"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

DELETE_BATCH_SIZE = 10
PACE_SECONDS_BETWEEN_CALLS = 0.22  # ~4.5 llamadas/seg, debajo del limite de Airtable


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if not YOUTUBE_API_KEY:
    die("Falta la variable de entorno YOUTUBE_API_KEY")
if not AIRTABLE_TOKEN:
    die("Falta la variable de entorno AIRTABLE_TOKEN")


# ---------------------------------------------------------------------------
# Helpers Airtable (con reintento simple ante rate limit 429)
# ---------------------------------------------------------------------------

def airtable_headers():
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def _request_with_retry(method, url, max_retries=5, **kwargs):
    for attempt in range(max_retries):
        resp = requests.request(method, url, timeout=60, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 30))
            print(f"Rate limit (429), esperando {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        return resp
    return resp


def airtable_list_all(table_id, filter_formula=None, fields=None):
    records = []
    params = {"pageSize": 100}
    if filter_formula:
        params["filterByFormula"] = filter_formula
    if fields:
        params["fields[]"] = fields
    offset = None
    while True:
        if offset:
            params["offset"] = offset
        resp = _request_with_retry(
            "GET",
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(PACE_SECONDS_BETWEEN_CALLS)
    return records


def airtable_create(table_id, records):
    created = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = _request_with_retry(
            "POST",
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
        )
        resp.raise_for_status()
        created.extend(resp.json().get("records", []))
        time.sleep(PACE_SECONDS_BETWEEN_CALLS)
    return created


def airtable_update(table_id, records):
    updated = []
    for i in range(0, len(records), 10):
        chunk = records[i : i + 10]
        resp = _request_with_retry(
            "PATCH",
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            json={"records": chunk, "typecast": True},
        )
        resp.raise_for_status()
        updated.extend(resp.json().get("records", []))
        time.sleep(PACE_SECONDS_BETWEEN_CALLS)
    return updated


def airtable_delete(table_id, record_ids):
    deleted_count = 0
    for i in range(0, len(record_ids), DELETE_BATCH_SIZE):
        chunk = record_ids[i : i + DELETE_BATCH_SIZE]
        params = [("records[]", rid) for rid in chunk]
        resp = _request_with_retry(
            "DELETE",
            f"{AIRTABLE_API}/{BASE_ID}/{table_id}",
            headers=airtable_headers(),
            params=params,
        )
        resp.raise_for_status()
        deleted_count += len(chunk)
        if deleted_count % 500 == 0:
            print(f"  ...borrados {deleted_count}/{len(record_ids)}")
        time.sleep(PACE_SECONDS_BETWEEN_CALLS)
    return deleted_count


def _parse_airtable_timestamp(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Helpers YouTube
# ---------------------------------------------------------------------------

def yt_get(path, **params):
    params["key"] = YOUTUBE_API_KEY
    resp = requests.get(f"{YOUTUBE_API}/{path}", params=params, timeout=30)
    if resp.status_code != 200:
        print(f"YouTube API error ({path}): {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def get_subscriber_counts(channel_ids):
    counts = {}
    for batch in chunked(channel_ids, 50):
        if not batch:
            continue
        data = yt_get("channels", part="statistics", id=",".join(batch))
        for item in data.get("items", []):
            stats = item.get("statistics", {})
            counts[item["id"]] = int(stats.get("subscriberCount", 0))
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now = datetime.datetime.utcnow()
    today_local_date_str = (now + datetime.timedelta(hours=ARGENTINA_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d")

    sources = airtable_list_all(TBL_VOD_SOURCES)
    if not sources:
        print("No hay fuentes VOD configuradas. Nada para hacer.")
        return

    sources_to_process = [
        s for s in sources if s["fields"].get("Last Daily Consolidation Date") != today_local_date_str
    ]
    if not sources_to_process:
        print("Todas las fuentes ya fueron consolidadas hoy. Nada para hacer.")
        return

    # 1) Suscriptores de las fuentes tipo Channel
    channel_source_ids = [
        s["fields"].get("Source ID", "").strip()
        for s in sources_to_process
        if s["fields"].get("Source Type") == "Channel" and s["fields"].get("Source ID")
    ]
    subscriber_counts = get_subscriber_counts(channel_source_ids) if channel_source_ids else {}

    # 2) Leer todos los videos VOD para saber a que fuente pertenece cada uno
    video_records = airtable_list_all(TBL_VOD_VIDEOS, fields=["Source"])
    video_source_by_record_id = {}
    for rec in video_records:
        links = rec["fields"].get("Source") or []
        if links:
            video_source_by_record_id[rec["id"]] = links[0]

    # 3) Leer todos los snapshots y agrupar por video
    print("Leyendo VOD Snapshots...")
    snapshot_records = airtable_list_all(TBL_VOD_SNAPSHOTS, fields=["Video", "Timestamp", "Views"])
    print(f"VOD Snapshots leidos: {len(snapshot_records)}")

    points_by_video = defaultdict(list)
    all_snapshot_ids = []
    for rec in snapshot_records:
        all_snapshot_ids.append(rec["id"])
        fields = rec["fields"]
        links = fields.get("Video") or []
        ts = fields.get("Timestamp")
        views = fields.get("Views")
        if not links or ts is None or views is None:
            continue
        dt = _parse_airtable_timestamp(ts)
        if dt is None:
            continue
        points_by_video[links[0]].append((dt.timestamp(), views))

    # 4) Calcular vistas ganadas por video, y agregar por fuente
    views_gained_by_source = defaultdict(int)
    videos_tracked_by_source = defaultdict(int)
    for video_record_id, points in points_by_video.items():
        source_record_id = video_source_by_record_id.get(video_record_id)
        if not source_record_id:
            continue
        points.sort(key=lambda p: p[0])
        if len(points) >= 2:
            delta = points[-1][1] - points[0][1]
        else:
            delta = 0  # 1 sola medicion: no hay referencia anterior, ver docstring
        views_gained_by_source[source_record_id] += max(delta, 0)
        videos_tracked_by_source[source_record_id] += 1

    # 5) Escribir VOD Daily + actualizar cada fuente
    daily_rows = []
    source_updates = []
    for source in sources_to_process:
        sid = source["id"]
        f = source["fields"]
        source_type = f.get("Source Type")
        source_id = (f.get("Source ID") or "").strip()

        daily_fields = {
            "Date": today_local_date_str,
            "Source": [sid],
            "Total Views Gained": views_gained_by_source.get(sid, 0),
            "Videos Tracked Count": videos_tracked_by_source.get(sid, 0),
        }

        source_fields_update = {"Last Daily Consolidation Date": today_local_date_str}

        if source_type == "Channel" and source_id in subscriber_counts:
            daily_fields["Subscribers"] = subscriber_counts[source_id]
            source_fields_update["Subscribers (last)"] = subscriber_counts[source_id]

        daily_rows.append({"fields": daily_fields})
        source_updates.append({"id": sid, "fields": source_fields_update})

    if daily_rows:
        airtable_create(TBL_VOD_DAILY, daily_rows)
    if source_updates:
        airtable_update(TBL_VOD_SOURCES, source_updates)

    # 6) Purgar VOD Snapshots (todos, no solo los de las fuentes procesadas hoy)
    if all_snapshot_ids:
        print(f"Borrando {len(all_snapshot_ids)} registros de VOD Snapshots...")
        deleted = airtable_delete(TBL_VOD_SNAPSHOTS, all_snapshot_ids)
        print(f"VOD Snapshots borrados: {deleted}")

    print(f"Listo. Fuentes consolidadas: {len(sources_to_process)}.")


if __name__ == "__main__":
    main()
