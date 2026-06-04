#!/usr/bin/env python3
import sys, time, json, urllib.parse, urllib.request

def analyze_path_and_stream(path, fast=False, stream_seconds=10):
    params = {'path': path, 'fast': 'true' if fast else 'false'}
    url = 'http://127.0.0.1:8000/analyze-path/?' + urllib.parse.urlencode(params)
    print('Calling', url)
    try:
        resp = urllib.request.urlopen(url, timeout=15)
        body = resp.read().decode()
    except Exception as e:
        print('Error calling analyze-path:', e)
        return
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    print('analyze-path response (truncated):', (body[:400] + '...') if len(body) > 400 else body)

    job_id = None
    if isinstance(payload, dict) and 'job_id' in payload:
        job_id = payload['job_id']
    # If server returned list of results immediately, no job to stream
    if not job_id:
        print('No job_id returned (synchronous results).')
        return

    prog_url = f'http://127.0.0.1:8000/progress/{job_id}'
    print('Streaming SSE from', prog_url)
    try:
        # open and stream lines for roughly stream_seconds
        u = urllib.request.urlopen(prog_url, timeout=stream_seconds+5)
        start = time.time()
        while True:
            line = u.readline()
            if not line:
                break
            s = line.decode().strip()
            if s:
                print('SSE:', s)
            if time.time() - start > stream_seconds:
                print('Stream time limit reached')
                break
        u.close()
    except Exception as e:
        print('Error streaming SSE:', e)


def post_file_and_stream(file_path, fast=False, stream_seconds=10):
    # Use requests if available for multipart upload and streaming
    try:
        import requests
    except Exception:
        print('`requests` not available; skipping file upload test.')
        return
    params = {'fast': 'true' if fast else 'false'}
    url = 'http://127.0.0.1:8000/analyze-multiple/'
    print('Posting file to', url, '->', file_path)
    try:
        with open(file_path, 'rb') as fh:
            files = [('files', (file_path.split('\\')[-1], fh, 'application/octet-stream'))]
            r = requests.post(url, params=params, files=files, timeout=30)
            print('upload response code', r.status_code)
            try:
                j = r.json()
                print('upload json:', j)
            except Exception:
                print('upload body:', r.text[:400])
                return
    except Exception as e:
        print('Error uploading file:', e)
        return
    job_id = j.get('job_id')
    if not job_id:
        print('No job_id returned from upload (server may have processed synchronously).')
        return
    prog_url = f'http://127.0.0.1:8000/progress/{job_id}'
    print('Streaming SSE for upload from', prog_url)
    try:
        r = requests.get(prog_url, stream=True, timeout=stream_seconds+5)
        start = time.time()
        for line in r.iter_lines():
            
            if line:
                print('SSE:', line.decode())
            if time.time() - start > stream_seconds:
                print('Stream time limit reached')
                break
        r.close()
    except Exception as e:
        print('Error streaming upload SSE:', e)

if __name__ == '__main__':
    # Usage: python scripts/test_sse.py <folder_path> <file_path>
    folder = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\naman\Desktop'
    filep = sys.argv[2] if len(sys.argv) > 2 else None
    analyze_path_and_stream(folder, fast=False, stream_seconds=10)
    if filep:
        post_file_and_stream(filep, fast=False, stream_seconds=10)
