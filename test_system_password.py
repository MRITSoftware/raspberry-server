import subprocess
import unittest
from unittest.mock import patch

from flask import Flask

from system_password import register_system_password


class SystemPasswordTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = 'test-only'
        register_system_password(app, 'panel-test-password')
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['authenticated'] = True
        self.user_patch = patch('system_password.service_username', return_value='mrit')
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)
        self.data = dict(panel_password='panel-test-password',
                         new_password='a-new-password:123', confirm_password='a-new-password:123')

    def headers(self):
        response = self.client.get('/api/system/password')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return {'X-CSRF-Token': response.json['csrf_token']}

    @patch('system_password.subprocess.run')
    def test_success_only_changes_service_user_through_stdin(self, run):
        run.return_value.returncode = 0
        headers = self.headers()
        response = self.client.post('/api/system/password', json={**self.data, 'username': 'root'}, headers=headers)
        self.assertEqual(response.status_code, 200)
        run.assert_called_once_with(['sudo', '-n', 'chpasswd'],
                                    input='mrit:a-new-password:123\n',
                                    capture_output=True, text=True, timeout=15)
        self.assertEqual(self.client.post('/api/system/password', json=self.data, headers=headers).status_code, 403)

    @patch('system_password.subprocess.run')
    def test_authentication_and_csrf_required(self, run):
        self.assertEqual(self.client.post('/api/system/password', json=self.data).status_code, 403)
        headers = self.headers()
        headers['Origin'] = 'http://other-site.example'
        self.assertEqual(self.client.post('/api/system/password', json=self.data, headers=headers).status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/api/system/password').status_code, 401)
        self.assertEqual(self.client.post('/api/system/password', json=self.data, headers=headers).status_code, 401)
        run.assert_not_called()

    @patch('system_password.subprocess.run')
    def test_rejects_bad_confirmation_and_password_injection(self, run):
        headers = self.headers()
        cases = [({'panel_password': 'wrong'}, 403),
                 ({'new_password': 'short'}, 400),
                 ({'new_password': 'valid-password\nroot:injected'}, 400),
                 ({'new_password': 'valid-password\x00'}, 400),
                 ({'confirm_password': 'does-not-match'}, 400)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                response = self.client.post('/api/system/password', json={**self.data, **changes}, headers=headers)
                self.assertEqual(response.status_code, expected)
        run.assert_not_called()

    @patch('system_password.subprocess.run')
    def test_unsupported_service_user_does_not_run_command(self, run):
        headers = self.headers()
        with patch('system_password.service_username', side_effect=RuntimeError('root forbidden')):
            self.assertEqual(self.client.get('/api/system/password').status_code, 503)
            response = self.client.post('/api/system/password', json=self.data, headers=headers)
            self.assertEqual(response.status_code, 503)
        run.assert_not_called()

    @patch('system_password.subprocess.run')
    def test_failures_do_not_return_process_output(self, run):
        headers = self.headers()
        run.return_value = subprocess.CompletedProcess([], 1, stdout='secret', stderr='secret')
        response = self.client.post('/api/system/password', json=self.data, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret', response.get_data(as_text=True))
        run.side_effect = subprocess.TimeoutExpired('chpasswd', 15, output='secret')
        response = self.client.post('/api/system/password', json=self.data, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
