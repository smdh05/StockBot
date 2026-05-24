import json
from pathlib import Path

cache_path = Path("db/instruments_cache.json")
if cache_path.exists():
    with open(cache_path, "r") as f:
        data = json.load(f)
        
    nifty_options = [
        inst for inst in data 
        if inst.get("exch_seg") == "NFO" and inst.get("name") == "NIFTY"
    ]
    
    expiries = sorted(list(set(inst.get("expiry") for inst in nifty_options)))
    print("Available expiries:")
    for exp in expiries[:20]:
        print(exp)
else:
    print("instruments_cache.json not found!")
