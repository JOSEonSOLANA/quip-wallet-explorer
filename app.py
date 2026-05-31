import os, json, threading, time
from flask import Flask, render_template, jsonify, request
from substrateinterface import SubstrateInterface

app = Flask(__name__)
VALIDATOR_URL = os.environ.get("QUIP_VALIDATOR_URL", "http://quip-validator:9944")
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/cache")
DEPTH = int(os.environ.get("SCAN_DEPTH", "50"))
TRACKED_FILE = os.path.join(CACHE_DIR, "tracked.json")

_running_scans = set()

def get_tracked():
    p = TRACKED_FILE
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return []

def save_tracked(address):
    tracked = get_tracked()
    if address not in tracked:
        tracked.insert(0, address)
        tracked = tracked[:50]
        os.makedirs(os.path.dirname(TRACKED_FILE), exist_ok=True)
        with open(TRACKED_FILE, "w") as f:
            json.dump(tracked, f)

def short_address(addr):
    return addr[:6] + "..." + addr[-4:]

def get_timestamp(substrate, block_num):
    try:
        result = substrate.query("Timestamp", "Now", block_hash=substrate.get_block_hash(block_num))
        if result and result.value:
            return int(result.value) // 1000
    except:
        pass
    return None

def get_events_for_block(substrate, block_num, address):
    results = []
    try:
        block_hash = substrate.get_block_hash(block_num)
        events = substrate.get_events(block_hash)
        ts = get_timestamp(substrate, block_num)
        for event in events:
            val = event.value
            mod = val.get("module_id", "")
            evt = val.get("event_id", "")
            attrs = val.get("attributes") or val.get("event", {}).get("attributes", {})
            attrs_lower = {k: str(v).lower() for k, v in attrs.items()}
            if not any(address.lower() in v for v in attrs_lower.values()):
                continue
            amount = None
            for ak in ("amount", "free_balance", "reward"):
                if ak in attrs:
                    try: amount = int(attrs[ak]) / 10**12
                    except: pass
                    break
            r = {"block": block_num, "ts": ts, "type": mod + "." + evt, "subtype": "info", "amount": amount}
            if mod == "Balances" and evt == "Transfer":
                src = str(attrs.get("from", ""))
                r["subtype"] = "outgoing" if address.lower() in src.lower() else "incoming"
                r["type"] = "transfer"
            elif mod == "Balances" and evt == "Endowed": r["type"] = "faucet"; r["subtype"] = "incoming"
            elif mod == "Balances" and evt == "Deposit": r["type"] = "deposit"; r["subtype"] = "incoming"
            elif mod == "Balances" and evt == "Withdraw": r["type"] = "fee"; r["subtype"] = "outgoing"
            elif mod == "QuantumPow" and evt == "ProofAccepted": r["type"] = "mining"; r["subtype"] = "incoming"
            elif mod == "QuantumPow" and evt == "BlockWinner": r["type"] = "block_winner"; r["subtype"] = "incoming"
            elif mod == "FaucetOps" and evt == "Minted": r["type"] = "faucet"; r["subtype"] = "incoming"
            elif mod == "System" and evt == "Remarked": r["type"] = "remark"; r["subtype"] = "info"
            results.append(r)
    except:
        pass
    return results

def cache_path(address):
    return os.path.join(CACHE_DIR, f"{address}.json")

def load_cache(address):
    p = cache_path(address)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

def save_cache(address, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path(address), "w") as f:
        json.dump(data, f)

def scan_background(address, start, end):
    global _running_scans
    try:
        save_cache(address, {"status": "scanning", "progress": 0, "scanned": 0, "total": end - start + 1, "events": []})
        substrate = SubstrateInterface(url=VALIDATOR_URL)
        all_events = []
        for bn in range(start, end + 1):
            try:
                e = get_events_for_block(substrate, bn, address)
                all_events.extend(e)
            except:
                pass
            scanned = bn - start + 1
            save_cache(address, {"status": "scanning", "progress": round(scanned / (end - start + 1) * 100, 1), "scanned": scanned, "total": end - start + 1, "events": all_events})
        substrate.close()
        save_cache(address, {"status": "complete", "progress": 100, "scanned": end - start + 1, "total": end - start + 1, "events": all_events})
    except Exception as e:
        save_cache(address, {"status": "error", "error": str(e), "events": []})
    finally:
        _running_scans.discard(address)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/ranking")
def api_ranking():
    tracked = get_tracked()
    if not tracked:
        return jsonify([])
    substrate = SubstrateInterface(url=VALIDATOR_URL)
    ranking = []
    for addr in tracked:
        try:
            result = substrate.query("System", "Account", [addr])
            if result and result.value:
                data = result.value
                free = int(data["data"]["free"])
                reserved = int(data["data"]["reserved"])
                total = (free + reserved) / 10**12
                ranking.append({"address": addr, "short": short_address(addr), "total": round(total, 4)})
        except:
            pass
    substrate.close()
    ranking.sort(key=lambda x: x["total"], reverse=True)
    return jsonify(ranking)

@app.route("/api/wallet/<address>")
def api_wallet(address):
    global _running_scans
    try:
        save_tracked(address)
        substrate = SubstrateInterface(url=VALIDATOR_URL)
        finalized = substrate.get_chain_finalised_head()
        header = substrate.get_block_header(finalized)
        current_block = header["header"]["number"]
        result = substrate.query("System", "Account", [address])
        if result is None or result.value is None:
            substrate.close()
            return jsonify({"error": "Account not found", "block": current_block}), 404
        data = result.value
        nonce = data["nonce"]
        free = int(data["data"]["free"])
        reserved = int(data["data"]["reserved"])
        substrate.close()

        cache = load_cache(address)
        if cache.get("status") in ("scanning", "complete"):
            return jsonify({
                "block": current_block, "address": address,
                "nonce": nonce, "free": free / 10**12, "reserved": reserved / 10**12, "total": (free + reserved) / 10**12,
                "cache": cache,
            })

        start = max(1, current_block - DEPTH)
        save_cache(address, {"status": "starting", "progress": 0, "scanned": 0, "total": current_block - start + 1, "events": []})
        _running_scans.add(address)
        t = threading.Thread(target=scan_background, args=(address, start, current_block), daemon=True)
        t.start()
        return jsonify({
            "block": current_block, "address": address,
            "nonce": nonce, "free": free / 10**12, "reserved": reserved / 10**12, "total": (free + reserved) / 10**12,
            "cache": {"status": "starting", "progress": 0, "scanned": 0, "total": current_block - start + 1, "events": []},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/scan-status/<address>")
def api_scan_status(address):
    return jsonify(load_cache(address))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)
