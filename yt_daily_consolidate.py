#!/usr/bin/env python3
"""
YouTube Streams Tracker -> Consolidacion diaria + purga de Snapshots

Corre UNA VEZ POR DIA (recomendado: 4am hora Argentina = 07:00 UTC), antes de
que arranque la ventana de monitoreo del dia (09:00 ART). Hace 2 cosas, en este
orden, para cada video que tenga registros en la tabla Snapshots:

  1) Consolida en el propio registro de Video (tabla Videos) un resumen
     permanente de esa transmision:
       - Avg Concurrents               (promedio de concurrentes durante el vivo)
       - Total Views                   (vistas finales)
       - Avg View Duration (min, est.) (ESTIMADO: minutos-espectador totales
         [integral de concurrentes en el tiempo] dividido vistas finales. Esto
         NO es el dato oficial de YouTube Analytics -- ese solo esta disponible
         via YouTube Analytics API y unicamente para canales propios. Es la
         mejor aproximacion posible con datos publicos.)
       - Net Subscribers               (suscriptores al final menos al inicio
         del vivo; puede ser negativo)
       - Concurrent Chart / Views Chart (graficos PNG de la evolucion, para que
         quede el historial visual aunque se borren los snapshots crudos)

     (Peak Concurrents / Peak Moment (min) / Peak Screenshot NO se tocan aca:
     esos se calculan en tiempo real dentro de yt_tracker.py, porque el
     screenshot del pico solo se puede capturar en el momento exacto en que
     ocurre -- a las 4am ya no hay forma de "volver" a ese instante del video.)

  2) Una vez consolidado TODO, borra TODOS los registros de la tabla Snapshots
     para liberar espacio y arrancar el dia siguiente con la tabla vacia.

Con volumenes grandes (cientos de miles de registros/dia) este proceso puede
tardar bastante (potencialmente 1-3 horas), sobre todo el borrado, porque la
API de Airtable solo permite borrar 10 registros por llamada y tiene un limite
de ~5 llamadas/segundo por base. Por eso conviene dejarlo corriendo en la
ventana ociosa (por ejemplo 4am a 9am) y no justo antes de que arranque el
monitoreo del dia.

Requisitos:
  pip install requests matplotlib

Variables de entorno requeridas: mismas que yt_tracker.py
  YOUTUBE_API_KEY   (no se usa aca, pero se valida por consistencia si esta seteada)
  AIRTABLE_TOKEN
"""

import os
import sys
import time
import base64
import datetime
from io import BytesIO
from collections import defaultdict

import requests

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ---------------------------------------------------------------------------
# Configuracion (mismos IDs que yt_tracker.py)
# ---------------------------------------------------------------------------

BASE_ID = "appFvqj21Yy73Bjcn"

TBL_VIDEOS = "tbl8JGhar3VxOinIB"
TBL_SNAPSHOTS = "tblMGC70UgtS7NBWG"

FLD_VIDEO_LINK_IN_SNAPSHOTS = "Video"
FLD_TIMESTAMP = "Timestamp"
FLD_CONCURRENT = "Concurrent Viewers"
FLD_VIEWS = "Views"
FLD_SUBSCRIBERS = "Subscribers"

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()

AIRTABLE_API = "https://api.airtable.com/v0"
AIRTABLE_CONTENT_API = "https://content.airtable.com/v0"

DELETE_BATCH_SIZE = 10
PACE_SECONDS_BETWEEN_CALLS = 0.22  # ~4.5 llamadas/seg, debajo del limite de Airtable


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


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


def airtable_upload_attachment(record_id, field_id_or_name, filename, content_type, file_bytes):
    b64 = base64.b64encode(file_bytes).decode("ascii")
    resp = _request_with_retry(
        "POST",
        f"{AIRTABLE_CONTENT_API}/{BASE_ID}/{record_id}/{field_id_or_name}/uploadAttachment",
        headers=airtable_headers(),
        json={"contentType": content_type, "file": b64, "filename": filename},
    )
    resp.raise_for_status()
    time.sleep(PACE_SECONDS_BETWEEN_CALLS)
    return resp.json()


def _parse_airtable_timestamp(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Graficos
# ---------------------------------------------------------------------------

def make_chart_png(timestamps, values, title, ylabel, color):
    fig, ax = plt.subplots(figsize=(8, 3.5), dpi=110)
    if timestamps and values:
        ax.plot(timestamps, values, color=color, linewidth=1.8)
        ax.fill_between(timestamps, values, color=color, alpha=0.15)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        fig.autofmt_xdate()
    ax.set_title(title, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Leyendo Snapshots (puede tardar varios minutos si hay muchos registros)...")
    snapshot_records = airtable_list_all(
        TBL_SNAPSHOTS,
        fields=[FLD_VIDEO_LINK_IN_SNAPSHOTS, FLD_TIMESTAMP, FLD_CONCURRENT, FLD_VIEWS, FLD_SUBSCRIBERS],
    )
    print(f"Snapshots leidos: {len(snapshot_records)}")

    if not snapshot_records:
        print("No hay snapshots para consolidar. Nada para hacer.")
        return

    # Agrupar por video (record id de la tabla Videos)
    by_video = defaultdict(list)
    all_snapshot_ids = []
    for rec in snapshot_records:
        all_snapshot_ids.append(rec["id"])
        fields = rec["fields"]
        links = fields.get(FLD_VIDEO_LINK_IN_SNAPSHOTS) or []
        if not links:
            continue
        ts = fields.get(FLD_TIMESTAMP)
        dt = _parse_airtable_timestamp(ts) if ts else None
        if dt is None:
            continue
        by_video[links[0]].append(
            {
                "epoch": dt.timestamp(),
                "dt": dt,
                "concurrent": fields.get(FLD_CONCURRENT),
                "views": fields.get(FLD_VIEWS),
                "subscribers": fields.get(FLD_SUBSCRIBERS),
            }
        )

    print(f"Videos distintos a consolidar: {len(by_video)}")

    video_updates = []
    processed = 0
    for video_record_id, points in by_video.items():
        points.sort(key=lambda p: p["epoch"])

        concurrent_points = [(p["epoch"], p["concurrent"]) for p in points if p["concurrent"] is not None]
        views_values = [p["views"] for p in points if p["views"] is not None]
        subs_values = [(p["epoch"], p["subscribers"]) for p in points if p["subscribers"] is not None]

        fields_update = {}

        if concurrent_points:
            avg_concurrents = sum(c for _, c in concurrent_points) / len(concurrent_points)
            fields_update["Avg Concurrents"] = round(avg_concurrents)

        total_views = max(views_values) if views_values else None
        if total_views is not None:
            fields_update["Total Views"] = total_views

        # Minutos-espectador totales, por integracion trapezoidal de concurrentes en el tiempo
        if len(concurrent_points) >= 2:
            person_minutes = 0.0
            for (t0, c0), (t1, c1) in zip(concurrent_points, concurrent_points[1:]):
                minutes = (t1 - t0) / 60.0
                if minutes > 0:
                    person_minutes += minutes * ((c0 + c1) / 2.0)
            if total_views:
                fields_update["Avg View Duration (min, est.)"] = round(person_minutes / total_views, 1)

        if len(subs_values) >= 2:
            fields_update["Net Subscribers"] = subs_values[-1][1] - subs_values[0][1]

        # Graficos
        try:
            if concurrent_points:
                png = make_chart_png(
                    [datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc) for t, _ in concurrent_points],
                    [c for _, c in concurrent_points],
                    "Concurrentes durante el vivo",
                    "Concurrentes",
                    "#2563eb",
                )
                airtable_upload_attachment(video_record_id, "Concurrent Chart", "concurrentes.png", "image/png", png)

            views_series = [(p["epoch"], p["views"]) for p in points if p["views"] is not None]
            if views_series:
                png = make_chart_png(
                    [datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc) for t, _ in views_series],
                    [v for _, v in views_series],
                    "Vistas durante el vivo",
                    "Vistas",
                    "#16a34a",
                )
                airtable_upload_attachment(video_record_id, "Views Chart", "vistas.png", "image/png", png)
        except requests.HTTPError as e:
            print(f"Aviso: no se pudo subir grafico para {video_record_id}: {e}", file=sys.stderr)

        if fields_update:
            video_updates.append({"id": video_record_id, "fields": fields_update})

        processed += 1
        if processed % 100 == 0:
            print(f"  ...consolidados {processed}/{len(by_video)} videos")

    if video_updates:
        print(f"Escribiendo resumenes en {len(video_updates)} registros de Videos...")
        airtable_update(TBL_VIDEOS, video_updates)

    print(f"Borrando {len(all_snapshot_ids)} registros de Snapshots (esto puede tardar)...")
    deleted = airtable_delete(TBL_SNAPSHOTS, all_snapshot_ids)
    print(f"Listo. Snapshots borrados: {deleted}")


if __name__ == "__main__":
    main()
