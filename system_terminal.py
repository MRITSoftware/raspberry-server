"""Terminal administrativo com saída limitada e execução fora da requisição HTTP."""

import hmac
import os
import secrets
import selectors
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit

from flask import jsonify, request, session

OUTPUT_LIMIT = 65536
UNLOCK_SECONDS = 600


def run_command(job, lock, cwd):
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            ['/bin/bash', '--noprofile', '--norc', '-c', job['command']],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + job['timeout']
        while True:
            if job['cancel'].is_set() or time.monotonic() >= deadline:
                with lock:
                    job['status'] = 'cancelled' if job['cancel'].is_set() else 'timeout'
                break
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    with lock:
                        combined = job['output'] + chunk
                        job['truncated'] |= len(combined) > OUTPUT_LIMIT
                        job['output'] = combined[-OUTPUT_LIMIT:]
            if process.poll() is not None and not selector.get_map():
                break
    except (OSError, ValueError):
        with lock:
            job['output'] = b'Nao foi possivel iniciar o comando.'
            job['status'] = 'error'
    finally:
        if process is not None:
            # Encerra também filhos que mantenham pipes abertos após timeout.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
        selector.close()
        with lock:
            job['exit_code'] = process.returncode if process else None
            if job['status'] == 'running':
                job['status'] = 'done'
            job['finished'] = True


def register_system_terminal(app, panel_password, cwd):
    jobs = {}
    lock = threading.Lock()

    def auth(write=False, unlocked=False):
        if not session.get('authenticated'):
            return jsonify(ok=False, error='Faça login no painel.'), 401
        if write:
            origin = request.headers.get('Origin')
            expected = urlsplit(request.host_url)._replace(path='').geturl()
            token = session.get('terminal_csrf', '')
            supplied = request.headers.get('X-CSRF-Token', '')
            if (origin and origin != expected) or not token or not hmac.compare_digest(
                token.encode(), supplied.encode()
            ):
                return jsonify(ok=False, error='Recarregue a página e tente novamente.'), 403
        if unlocked and session.get('terminal_until', 0) < time.time():
            return jsonify(ok=False, error='Confirme a senha do painel para liberar o terminal.'), 403
        return None

    @app.get('/api/system/terminal')
    def terminal_info():
        denied = auth()
        if denied:
            return denied
        if os.name != 'posix':
            return jsonify(ok=False, error='Terminal disponível somente no Raspberry/Linux.'), 503
        session.setdefault('terminal_csrf', secrets.token_urlsafe(32))
        response = jsonify(ok=True, csrf_token=session['terminal_csrf'],
                           unlocked=session.get('terminal_until', 0) > time.time())
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/system/terminal/unlock')
    def terminal_unlock():
        denied = auth(write=True)
        if denied:
            return denied
        data = request.get_json(silent=True)
        password = data.get('password') if isinstance(data, dict) else None
        if not isinstance(password, str) or not hmac.compare_digest(password.encode(), panel_password.encode()):
            return jsonify(ok=False, error='Senha do painel incorreta.'), 403
        session['terminal_until'] = time.time() + UNLOCK_SECONDS
        return jsonify(ok=True)

    @app.post('/api/system/terminal/execute')
    def terminal_execute():
        denied = auth(write=True, unlocked=True)
        if denied:
            return denied
        if os.name != 'posix':
            return jsonify(ok=False, error='Terminal disponível somente no Raspberry/Linux.'), 503
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error='Dados inválidos.'), 400
        command = data.get('command')
        timeout = data.get('timeout', 30)
        if not isinstance(command, str) or not command.strip() or len(command) > 8192 or '\x00' in command:
            return jsonify(ok=False, error='Informe um comando de até 8192 caracteres.'), 400
        if type(timeout) is not int or timeout not in (10, 30, 120):
            return jsonify(ok=False, error='Tempo limite inválido.'), 400
        with lock:
            if any(not job['finished'] for job in jobs.values()):
                return jsonify(ok=False, error='Já existe um comando em execução. Aguarde ou cancele.'), 409
            # Não persiste comandos nem saída no disco ou nos logs.
            jobs.clear()
            job_id = secrets.token_urlsafe(24)
            job = dict(command=command, timeout=timeout, owner=session['terminal_csrf'],
                       output=b'', status='running', exit_code=None, truncated=False,
                       cancel=threading.Event(), finished=False)
            jobs[job_id] = job
        threading.Thread(target=run_command, args=(job, lock, cwd), daemon=True).start()
        return jsonify(ok=True, job_id=job_id), 202

    @app.route('/api/system/terminal/jobs/<job_id>', methods=['GET', 'DELETE'])
    def terminal_job(job_id):
        denied = auth(write=request.method == 'DELETE', unlocked=True)
        if denied:
            return denied
        with lock:
            job = jobs.get(job_id)
            if not job or job['owner'] != session.get('terminal_csrf'):
                return jsonify(ok=False, error='Execução não encontrada.'), 404
            if request.method == 'DELETE':
                job['cancel'].set()
            response = jsonify(ok=True, output=job['output'].decode('utf-8', errors='replace'),
                               status=job['status'], exit_code=job['exit_code'],
                               truncated=job['truncated'], finished=job['finished'])
        response.headers['Cache-Control'] = 'no-store'
        return response
