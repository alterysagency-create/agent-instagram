"""Run the HTTP API and the queue worker in one small deployment."""
import os
import signal
import subprocess
import sys
import time


def main():
    port = os.getenv('PORT', '10000')
    processes = [
        subprocess.Popen([sys.executable, '-m', 'services.worker']),
        subprocess.Popen([
            'gunicorn', '--bind', f'0.0.0.0:{port}', '--workers', '1',
            '--timeout', '60', 'app:app'
        ]),
    ]

    def stop(*_):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    stop()
                    return code or 1
            time.sleep(1)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == '__main__':
    raise SystemExit(main())
