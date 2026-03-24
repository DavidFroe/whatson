import json, os
from datetime import datetime

store_dir = os.path.expanduser("~/.whatson/store")
if not os.path.exists(store_dir):
    print("no store")
    exit(0)


def parse_metadata(m):
    d_str, t_str, sender = m.get("date", ""), m.get("time", ""), m.get("sender", "")
    dt = datetime.min
    raw = str(m.get("pre") or m.get("time", "")).replace("[", "").strip()
    
    if "date" not in m and "]" in raw:
        try:
            left, right = raw.split("]", 1)
            sender = right.strip(" :")
            time_part, _, date_part = left.partition(",")
            t_str = time_part.strip()
            d_str = date_part.strip()
            
            parts = d_str.split(".")
            if len(parts) == 3 and len(parts[2]) == 2:
                parts[2] = "20" + parts[2]
                d_str = ".".join(parts)
        except Exception:
            pass
            
    try:
        if d_str and t_str:
            dt = datetime.strptime(f"{d_str} {t_str}", "%d.%m.%Y %H:%M")
    except Exception:
        pass
        
    return d_str, t_str, sender, dt

for folder in os.listdir(store_dir):
    msg_path = os.path.join(store_dir, folder, "messages.json")
    if os.path.exists(msg_path):
        with open(msg_path, "r", encoding="utf-8") as f:
            try:
                msgs = json.load(f)
            except Exception:
                continue
                
        existing_hashes = set()
        deduped = []
        for m in msgs:
            d, t, s, dt = parse_metadata(m)
            h = f"{t}|{s}|{m.get('text', '').strip()}"
            if h not in existing_hashes:
                existing_hashes.add(h)
                m["_parsed_dt"] = dt.isoformat()
                deduped.append(m)
                
        def skey(m):
            return datetime.fromisoformat(m["_parsed_dt"])
            
        deduped.sort(key=skey)
        for m in deduped:
            del m["_parsed_dt"]
            
        with open(msg_path, "w", encoding="utf-8") as f:
            json.dump(deduped, f, indent=2, ensure_ascii=False)
        print(f"Fixed {folder} - {len(msgs)} -> {len(deduped)}")
