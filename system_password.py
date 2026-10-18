"""Troca restrita da senha Linux pelo painel autenticado."""

import hmac
import os
import secrets
import subprocess
from urllib.parse import urlsplit

from flask import jsonify, request, session


def service_username():
    import pwd

    if os.geteuid() == 0:
        raise RuntimeError("Troca pelo painel indisponível quando o serviço roda como root.")
    return pwd.getpwuid(os.geteuid()).pw_name


def register_system_password(app, panel_password):
    @app.get('/api/system/password')
    def system_password_info():
        if not session.get('authenticated'):
            return jsonify(ok=False, error='Autenticação necessária.'), 401
        try:
            username = service_username()
        except (ImportError, RuntimeError, KeyError):
            return jsonify(ok=False, error='Troca de senha indisponível neste sistema.'), 503
        token = session.setdefault('password_csrf', secrets.token_urlsafe(32))
        response = jsonify(ok=True, username=username, csrf_token=token)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post('/api/system/password')
    def system_password_change():
        if not session.get('authenticated'):
            return jsonify(ok=False, error='Autenticação necessária.'), 401
        origin = request.headers.get('Origin')
        if origin and origin != urlsplit(request.host_url)._replace(path='').geturl():
            return jsonify(ok=False, error='Origem inválida.'), 403
        token = session.get('password_csrf', '')
        if not token or not hmac.compare_digest(
            token.encode('utf-8'), request.headers.get('X-CSRF-Token', '').encode('utf-8')
        ):
            return jsonify(ok=False, error='Recarregue a página e tente novamente.'), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error='Dados inválidos.'), 400
        confirmation = data.get('panel_password')
        if not isinstance(confirmation, str) or not hmac.compare_digest(
            confirmation.encode('utf-8'), panel_password.encode('utf-8')
        ):
            return jsonify(ok=False, error='Senha do painel incorreta.'), 403
        password = data.get('new_password')
        if (not isinstance(password, str) or not 12 <= len(password) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in password)):
            return jsonify(ok=False, error='Use de 12 a 128 caracteres, sem quebras de linha ou caracteres de controle.'), 400
        if password != data.get('confirm_password'):
            return jsonify(ok=False, error='As novas senhas não conferem.'), 400
        try:
            username = service_username()
            # Senha somente no stdin: nunca na linha de comando ou nos logs.
            result = subprocess.run(
                ['sudo', '-n', 'chpasswd'], input=f'{username}:{password}\n',
                capture_output=True, text=True, timeout=15,
            )
        except (ImportError, RuntimeError, KeyError, OSError, subprocess.SubprocessError):
            return jsonify(ok=False, error='Não foi possível concluir a troca. Verifique o acesso SSH antes de tentar novamente.'), 503
        if result.returncode != 0:
            return jsonify(ok=False, error='O sistema recusou a troca. Verifique as permissões e a política de senha.'), 503
        session.pop('password_csrf', None)
        return jsonify(ok=True, message=f'Senha do usuário {username} alterada. Use a nova senha no SSH.')
