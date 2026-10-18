import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask

from system_terminal import OUTPUT_LIMIT, register_system_terminal, run_command


class TerminalApiTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = 'test-only'
        register_system_terminal(app, 'test-password', os.getcwd())
        self.client = app.test_client()
        self.other = app.test_client()
        for client in (self.client, self.other):
            with client.session_transaction() as session:
                session['authenticated'] = True
                session['terminal_csrf'] = 'csrf-' + str(id(client))
        self.headers = {'X-CSRF-Token': 'csrf-' + str(id(self.client))}

    def unlock(self):
        return self.client.post('/api/system/terminal/unlock', json={'password': 'test-password'}, headers=self.headers)

    def execute(self, **data):
        with patch('system_terminal.os.name', 'posix'):
            return self.client.post('/api/system/terminal/execute', json=data, headers=self.headers)

    @patch('system_terminal.threading.Thread')
    def test_requires_login_csrf_and_password(self, thread):
        self.assertEqual(self.execute(command='id').status_code, 403)
        self.assertEqual(self.client.post('/api/system/terminal/unlock', json={'password': 'test-password'}).status_code, 403)
        self.assertEqual(self.client.post('/api/system/terminal/unlock', json={'password': 'wrong'}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post('/api/system/terminal/unlock', json={'password': 'test-password'}, headers={**self.headers, 'Origin': 'http://other.example'}).status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/api/system/terminal').status_code, 401)
        self.assertEqual(self.execute(command='id').status_code, 401)
        thread.assert_not_called()

    @patch('system_terminal.threading.Thread')
    def test_job_ownership_concurrency_and_cancellation(self, thread):
        self.assertEqual(self.unlock().status_code, 200)
        response = self.execute(command='hostname', timeout=10)
        self.assertEqual(response.status_code, 202)
        path = '/api/system/terminal/jobs/' + response.json['job_id']
        self.assertEqual(self.client.get(path).json['status'], 'running')
        self.assertEqual(self.execute(command='id').status_code, 409)
        with self.other.session_transaction() as session:
            session['terminal_until'] = time.time() + 100
        self.assertEqual(self.other.get(path).status_code, 404)
        self.assertEqual(self.client.delete(path).status_code, 403)
        self.assertEqual(self.client.delete(path, headers=self.headers).status_code, 200)
        job = thread.call_args.kwargs['args'][0]
        self.assertTrue(job['cancel'].is_set())
        job['status'] = 'done'
        job['finished'] = True
        job['output'] = b'<script>example</script>'
        self.assertEqual(self.client.get(path).json['output'], '<script>example</script>')
        self.assertEqual(self.execute(command='id').status_code, 202)
        self.assertEqual(self.client.get(path).status_code, 404)

    @patch('system_terminal.threading.Thread')
    def test_input_validation_and_expiration(self, thread):
        self.unlock()
        for payload in ({'command': ''}, {'command': 'x' * 8193}, {'command': 'id\x00'},
                        {'command': 'id', 'timeout': 999}, {'command': 'id', 'timeout': True}):
            self.assertEqual(self.execute(**payload).status_code, 400)
        with self.client.session_transaction() as session:
            session['terminal_until'] = time.time() - 1
        self.assertEqual(self.execute(command='id').status_code, 403)
        thread.assert_not_called()


@unittest.skipUnless(os.name == 'posix', 'Worker requer Linux')
class TerminalWorkerTests(unittest.TestCase):
    def run_job(self, command, timeout=3, cancel=False):
        job = dict(command=command, timeout=timeout, output=b'', status='running',
                   exit_code=None, truncated=False, cancel=threading.Event(), finished=False)
        if cancel:
            job['cancel'].set()
        with tempfile.TemporaryDirectory() as cwd:
            run_command(job, threading.Lock(), cwd)
        self.assertTrue(job['finished'])
        return job

    def test_stdout_stderr_exit_code_and_no_interactive_input(self):
        job = self.run_job("printf 'hello'; printf 'error' >&2; exit 7")
        self.assertEqual(job['output'], b'helloerror')
        self.assertEqual(job['exit_code'], 7)
        self.assertEqual(job['status'], 'done')
        self.assertEqual(self.run_job('cat')['exit_code'], 0)

    def test_timeout_and_cancel_kill_child_with_open_pipe(self):
        self.assertEqual(self.run_job('sleep 10 & wait', timeout=0.1)['status'], 'timeout')
        self.assertEqual(self.run_job('sleep 10', cancel=True)['status'], 'cancelled')

    def test_output_is_bounded(self):
        job = self.run_job('head -c 100000 /dev/zero')
        self.assertEqual(len(job['output']), OUTPUT_LIMIT)
        self.assertTrue(job['truncated'])


if __name__ == '__main__':
    unittest.main()
