import json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import api, config

library_id = "sclibrary"
user = os.environ["SCL_USER"]
password = os.environ["SCL_PASSWORD"]

print("Logging in...")
bc_token, session_id, account_id = api.login(library_id, user, password)
print(f"account_id={account_id}")

print("\nFetching history (page=0)...")
hist_resp = api.proxy_fetch_history(library_id, bc_token, session_id, account_id, page=0)
print(json.dumps(hist_resp, indent=2)[:3000])
