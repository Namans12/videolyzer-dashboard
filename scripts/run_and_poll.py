import subprocess
import json
import time
import urllib.request

# POST using native curl
p = subprocess.run(["curl.exe", "-F", "files=@test.mp4", "http://127.0.0.1:8000/analyze-multiple/?fast=false"], capture_output=True, text=True)
out = p.stdout.strip()
# find first JSON object in output
start = out.find('{')
if start == -1:
    print('no-json', out)
    raise SystemExit(1)
js = out[start:]
resp = json.loads(js)
job_id = resp.get('job_id')
print('job_id', job_id)

url = f'http://127.0.0.1:8000/job/{job_id}'
for i in range(60):
    try:
        with urllib.request.urlopen(url) as resp2:
            data = json.load(resp2)
    except Exception as e:
        print('error', e)
        break
    print(data.get('current'), '—', data.get('progress'), '—', data.get('status'))
    if data.get('status') == 'done':
        print(json.dumps(data))
        break
    time.sleep(0.6)
