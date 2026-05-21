import json
import os
import time

STATS_PATH = "stats.json"


def _load() -> dict:
    if os.path.exists(STATS_PATH):
        with open(STATS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"forms": []}


def _save(data: dict):
    with open(STATS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def record_form(mod_id: int, form_type: str, status: str):
    if not mod_id:
        return
    data = _load()
    data["forms"].append({
        "mod_id": mod_id,
        "type": form_type,
        "status": status,
        "ts": int(time.time()),
    })
    _save(data)


def get_stats(mod_id: int, since_ts: int = 0) -> dict:
    data = _load()
    result = {"sent": 0, "approved": 0, "rejected": 0}
    for f in data["forms"]:
        if f["mod_id"] == mod_id and f["ts"] >= since_ts:
            status = f.get("status", "sent")
            if status in result:
                result[status] += 1
    return result
