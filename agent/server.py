"""只监听loopback的演示界面与受限转发，不代理任意URL。"""
import concurrent.futures
import json
import mimetypes
import os
import pathlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .worker import JavaClient, ApiError, run_task

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'web'
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
JOBS = {}
VIEWER = os.environ.get('CONFIG_DOCTOR_VIEWER_TOKEN',os.environ.get('VIEWER_TOKEN','viewer-token'))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, status, value, content_type='application/json'):
        data = json.dumps(value,ensure_ascii=False).encode() if content_type=='application/json' else value
        self.send_response(status)
        self.send_header('Content-Type',content_type+'; charset=utf-8')
        self.send_header('Content-Length',str(len(data)))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get('Host') in ('127.0.0.1:18081','localhost:18081')

    def do_GET(self):
        if not self.valid_host():
            return self.send(403,{'error':'invalid host'})
        path = self.path.split('?')[0]
        try:
            if path in ('/api/tasks','/api/cases','/api/health') or re.fullmatch(r'/api/tasks/[\w-]+',path):
                return self.send(200,JavaClient(VIEWER).request('GET',path))
            if re.fullmatch(r'/jobs/[\w-]+',path):
                future = JOBS.get(path.split('/')[-1])
                return self.send(200,{'running':bool(future and not future.done())})
            allowed = {'/':'index.html','/app.js':'app.js','/app.css':'app.css'}
            if path not in allowed:
                return self.send(404,{'error':'not found'})
            file = ROOT / allowed[path]
            return self.send(200,file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'text/plain')
        except ApiError as error:
            self.send(error.status,{'error':str(error)})
        except Exception as error:
            self.send(502,{'error':type(error).__name__})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin not in ('http://127.0.0.1:18081','http://localhost:18081')):
            return self.send(403,{'error':'same-origin only'})
        if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
            return self.send(415,{'error':'JSON required'})
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 16384:
                return self.send(413,{'error':'body size invalid'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                return self.send(400,{'error':'object required'})
            if self.path == '/api/tasks':
                return self.send(200,JavaClient(VIEWER).request('POST',self.path,data))
            if re.fullmatch(r'/run/[\w-]+',self.path):
                task_id = self.path.split('/')[-1]
                existing = JOBS.get(task_id)
                if existing and not existing.done():
                    return self.send(409,{'error':'worker already running'})
                if sum(not future.done() for future in JOBS.values()) >= 2:
                    return self.send(429,{'error':'two worker limit'})
                if not os.environ.get('DEEPSEEK_API_KEY'):
                    return self.send(503,{'error':'DEEPSEEK_API_KEY not configured'})
                JOBS[task_id] = POOL.submit(run_task,task_id)
                return self.send(202,{'taskId':task_id,'running':True})
            if self.path == '/demo':
                token = data.get('operatorToken','')
                if not token:
                    return self.send(403,{'error':'operator token required'})
                case_id = 'demo-'+os.urandom(4).hex()
                result = JavaClient(token).request('POST','/api/cases',{'id':case_id,
                    'symptom':'结算偶发失败，请根据运行证据定位问题。',
                    'config':{'upstreamDelayMs':120,'timeoutMs':30,'cachePrice':100,'sourcePrice':100,'capacity':10,'taxRate':0.06}})
                return self.send(200,result)
            if re.fullmatch(r'/api/tasks/[\w-]+/approve',self.path):
                token = data.pop('operatorToken','')
                if not token:
                    return self.send(403,{'error':'operator token required'})
                return self.send(200,JavaClient(token).request('POST',self.path,data))
            return self.send(404,{'error':'not found'})
        except ApiError as error:
            self.send(error.status,{'error':str(error)})
        except (ValueError,TypeError):
            self.send(400,{'error':'invalid JSON'})
        except Exception as error:
            self.send(502,{'error':type(error).__name__})


if __name__ == '__main__':
    print('Config Doctor: http://127.0.0.1:18081',flush=True)
    ThreadingHTTPServer(('127.0.0.1',18081),Handler).serve_forever()
