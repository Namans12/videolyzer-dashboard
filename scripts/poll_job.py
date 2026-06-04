import time
import urllib.request
import json

job_id = 'a2e973ec805341f89d7bb7a7ccf858d0'
url = f'http://127.0.0.1:8000/job/{job_id}'

for i in range(40):
    try:
        with urllib.request.urlopen(url) as resp:
            data = json.load(resp)
    except Exception as e:
        print('error', e)
        break
    print(data.get('current'), '—', data.get('progress'), '—', data.get('status'))
    if data.get('status') == 'done':
        print(json.dumps(data))
        break
    time.sleep(0.6)
