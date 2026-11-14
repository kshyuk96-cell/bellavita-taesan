"""Crash-released writer serialization for processes sharing the same SQLite file."""
import contextlib,os,threading
from pathlib import Path

_MEMORY_LOCK=threading.RLock()

@contextlib.contextmanager
def writer_lock(db):
    with db() as c:
        filename=c.execute('PRAGMA database_list').fetchone()[2]
    if not filename:
        with _MEMORY_LOCK:yield
        return
    path=str(Path(filename).resolve())+'.order-writer.lock'
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX)
        try:yield
        finally:
            if os.name=='nt':os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
    finally:os.close(fd)
