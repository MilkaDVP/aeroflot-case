"""
Граф аэропорта из геоданных GeoJSON — например, испытательного полигона.

Пример (полигон ВДНХ, геоданные лежат в data/geodata/vdnh):

    python data/import_geodata.py data/geodata/vdnh VDNH "Полигон ВДНХ" Москва

Результат — data/airports/<КОД>.json; сервер подхватит его при запуске.
Разбор слоёв и правила допуска агентов — server/services/geodata_import.py.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from services.geodata_import import build_payload  # noqa: E402


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(1)
    directory, code, name, city = sys.argv[1:5]
    payload = build_payload(directory, code.upper(), name, city)
    target = ROOT / "data" / "airports" / f"{code.upper()}.json"
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    kinds = {}
    for edge in payload["edges"]:
        kinds[edge["kind"]] = kinds.get(edge["kind"], 0) + 1
    print(f"{target.name}: вершин {len(payload['nodes'])}, рёбер {len(payload['edges'])} {kinds}, "
          f"зон {len(payload['zones'])}, объектов {len(payload['objects'])}, "
          f"пришито концов {payload['stats']['snapped_ends']}")


if __name__ == "__main__":
    main()
