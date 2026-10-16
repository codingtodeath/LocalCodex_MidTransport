import subprocess
import pytest
from windows import local_control


def test_windows_start_delegates_to_os_scheduler_without_direct_worker_child(monkeypatch,tmp_path):
    seen=[]
    monkeypatch.setattr(local_control.os,'name','nt')
    def run(command,**kwargs):
        seen.append((command,kwargs))
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(local_control.subprocess,'run',run)
    monkeypatch.setattr(local_control.subprocess,'Popen',lambda *a,**k:pytest.fail('direct_worker_child'))
    assert local_control.launch_worker(tmp_path/'worker.json') is None
    command,kwargs=seen[0]
    assert 'Start-DetachedWorker.ps1' in command[command.index('-File')+1]
    assert command[command.index('-Python')+1]==local_control.sys.executable
    assert kwargs['timeout']==20 and kwargs['capture_output']


def test_scheduler_failure_never_falls_back_to_parent_owned_process(monkeypatch,tmp_path):
    monkeypatch.setattr(local_control.os,'name','nt')
    monkeypatch.setattr(local_control.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a[0],1))
    monkeypatch.setattr(local_control.subprocess,'Popen',lambda *a,**k:pytest.fail('unsafe_fallback'))
    with pytest.raises(RuntimeError,match='worker_detached_start_failed'):
        local_control.launch_worker(tmp_path/'worker.json')
