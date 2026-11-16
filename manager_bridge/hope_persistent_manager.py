"""Start/restart the real manager in a separate trusted Python process, no shell."""
import os
from pathlib import Path
import subprocess
import sys
import time


class PersistentManager:
    def __init__(self,root,database):
        self.root=Path(root);self.database=Path(database);self.ready=self.database.with_suffix('.ready');self.process=None
    def start(self):
        if self.process is not None:raise ValueError('already running')
        self.ready.unlink(missing_ok=True)
        self.process=subprocess.Popen([sys.executable,'-m','manager_bridge.hope_local_manager','--database',str(self.database),'--ready-file',str(self.ready)],cwd=self.root,
            env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'PYTHONDONTWRITEBYTECODE':'1'},stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        until=time.monotonic()+10
        while time.monotonic()<until:
            if self.process.poll() is not None:break
            if self.ready.is_file():
                port=int(self.ready.read_text());self.base='http://127.0.0.1:'+str(port);return self.base
            time.sleep(.05)
        self.stop();raise RuntimeError('nonproduction manager failed to start')
    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
            self.process=None
        self.ready.unlink(missing_ok=True)
    def restart(self):self.stop();return self.start()
    def __enter__(self):self.start();return self
    def __exit__(self,*_):self.stop()
